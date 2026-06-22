#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Apollo.io verification and enrollment for captured school-security contacts.

When a call captures the person in charge of safety/security at a school or
district, server.py hands the outcome row here. We:

1. Enrich the person against Apollo (People Match), which returns the school's
   official website/domain plus Apollo's verification of the email and any
   phone number it already has on file.
2. Validate the email the caller gave us against the school/district website
   domain, flagging a match or mismatch.
3. Create (or update) the contact in Apollo with the verified details and add
   them to an outreach sequence so a RunScout teammate follows up by email and
   phone.

Everything is best effort: a failure is logged, never raised, so it can't break
call-result recording. The whole integration is opt-in: nothing runs unless
APOLLO_API_KEY is set.
"""

import os

import aiohttp
from loguru import logger

APOLLO_BASE_URL = "https://api.apollo.io/api/v1"

# Defaults wire the app to the "Runscout School Safety — K-12 Outreach" sequence,
# sending from xiomara@runscout.ai, enrolling contacts paused for review. Each is
# overridable from the environment.
DEFAULT_SEQUENCE_ID = "6a343ebde13d870014734823"
DEFAULT_EMAIL_ACCOUNT_ID = "6a17148fcb8db60014f18f9b"
DEFAULT_ENROLL_STATUS = "paused"  # "paused" (held for review) or "active" (sends now)


def _split_name(full_name: str) -> tuple[str, str]:
    """Split a full name into (first, last). Last name may be empty."""
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _domain_of_email(email: str) -> str:
    return email.split("@", 1)[1].lower().strip() if email and "@" in email else ""


def _domain_of_url(url: str) -> str:
    """Bare registrable host for a website URL, e.g. https://www.lincoln.org/ -> lincoln.org."""
    if not url:
        return ""
    host = url.strip().lower()
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    host = host.split("/", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": api_key,
    }


def _format_phone(phone: str, extension: str) -> str:
    """Combine a phone number with any extension, e.g. '555-123-4567 x208'."""
    phone = (phone or "").strip()
    extension = (extension or "").strip()
    if phone and extension:
        return f"{phone} x{extension}"
    return phone or extension


async def _match_person(
    session: aiohttp.ClientSession, api_key: str, row: dict[str, str]
) -> dict | None:
    """Enrich the captured person via Apollo People Match (costs 1 credit if found).

    Returns the matched ``person`` object (with verified email status, the
    school organization and its website, and any phone Apollo has on file), or
    None if no match / on error.
    """
    first_name, last_name = _split_name(row.get("contact_name", ""))
    payload: dict[str, object] = {
        "first_name": first_name,
        "last_name": last_name,
        "organization_name": row.get("lead_company", ""),
        "reveal_personal_emails": True,
    }
    if row.get("contact_email"):
        payload["email"] = row["contact_email"]
    payload = {k: v for k, v in payload.items() if v}

    try:
        async with session.post(
            f"{APOLLO_BASE_URL}/people/match", headers=_headers(api_key), json=payload
        ) as resp:
            if resp.status not in (200, 201):
                logger.warning(f"Apollo people match failed ({resp.status}): {await resp.text()}")
                return None
            data = await resp.json()
            return data.get("person")
    except Exception as e:
        logger.warning(f"Apollo people match error: {e}")
        return None


def _verify(row: dict[str, str], person: dict | None) -> dict[str, str]:
    """Cross-check the captured email/phone against Apollo + the school website.

    Returns the values to store on the contact plus a human-readable note that
    records what was validated.
    """
    captured_email = (row.get("contact_email") or "").strip()
    captured_phone = (row.get("contact_phone") or "").strip()

    verified_email = captured_email
    email_status = "unchecked"
    school_website = ""
    school_domain = ""
    apollo_phone = ""

    if person:
        org = person.get("organization") or {}
        school_website = org.get("website_url") or ""
        school_domain = (org.get("primary_domain") or _domain_of_url(school_website)).lower()
        email_status = person.get("email_status") or "unknown"
        # Prefer Apollo's email when it has a confirmed one and we either lack
        # one or Apollo marks it verified.
        apollo_email = (person.get("email") or "").strip()
        if apollo_email and (not captured_email or email_status == "verified"):
            verified_email = apollo_email
        phones = person.get("phone_numbers") or []
        if phones:
            apollo_phone = phones[0].get("sanitized_number") or phones[0].get("raw_number") or ""

    # Validate the email domain against the school/district website domain.
    email_domain = _domain_of_email(verified_email)
    if email_domain and school_domain:
        domain_match = "yes" if email_domain == school_domain else "no"
    else:
        domain_match = "unknown"

    site_label = school_domain or school_website or "n/a"
    note = (
        f"Apollo verify: email_status={email_status}; "
        f"website={site_label}; email_domain_match={domain_match}"
    )
    if apollo_phone:
        note += f"; apollo_phone={apollo_phone}"

    return {
        "email": verified_email,
        "phone": apollo_phone or captured_phone,
        "school_website": school_website,
        "domain_match": domain_match,
        "note": note,
    }


async def _create_or_update_contact(
    session: aiohttp.ClientSession,
    api_key: str,
    row: dict[str, str],
    verified: dict[str, str],
    account_id: str | None = None,
) -> str | None:
    """Create (or update, if Apollo matches an existing record) the contact."""
    first_name, last_name = _split_name(row.get("contact_name", ""))
    payload = {
        "first_name": first_name,
        "last_name": last_name,
        "title": row.get("contact_role", ""),
        "email": verified.get("email", ""),
        "direct_phone": _format_phone(verified.get("phone", ""), row.get("contact_extension", "")),
        # The school or district name rides along on the lead's "company" field.
        "organization_name": row.get("lead_company", ""),
        "website_url": verified.get("school_website", ""),
        # Link the contact to the school's tracked Account when we have one.
        "account_id": account_id or "",
        "label_names": ["RunScout School Security Bot"],
    }
    # Drop empty fields so we don't overwrite existing Apollo data with blanks.
    payload = {k: v for k, v in payload.items() if v}

    async with session.post(
        f"{APOLLO_BASE_URL}/contacts", headers=_headers(api_key), json=payload
    ) as resp:
        if resp.status not in (200, 201):
            logger.warning(f"Apollo contact create failed ({resp.status}): {await resp.text()}")
            return None
        data = await resp.json()
        return (data.get("contact") or {}).get("id")


async def _add_to_sequence(
    session: aiohttp.ClientSession, api_key: str, contact_id: str, has_email: bool
) -> bool:
    """Add the contact to the configured Apollo sequence. Returns True on success."""
    sequence_id = os.getenv("APOLLO_SEQUENCE_ID", DEFAULT_SEQUENCE_ID)
    email_account_id = os.getenv("APOLLO_EMAIL_ACCOUNT_ID", DEFAULT_EMAIL_ACCOUNT_ID)
    status = os.getenv("APOLLO_ENROLL_STATUS", DEFAULT_ENROLL_STATUS)

    payload = {
        "emailer_campaign_id": sequence_id,
        "contact_ids": [contact_id],
        "send_email_from_email_account_id": email_account_id,
        "status": status,
        # Let contacts in without a verified/known email so phone-only leads still
        # land in the sequence for the team's call steps.
        "sequence_no_email": not has_email,
        "sequence_unverified_email": True,
    }
    async with session.post(
        f"{APOLLO_BASE_URL}/emailer_campaigns/{sequence_id}/add_contact_ids",
        headers=_headers(api_key),
        json=payload,
    ) as resp:
        if resp.status not in (200, 201):
            logger.warning(f"Apollo sequence add failed ({resp.status}): {await resp.text()}")
            return False
        return True


async def _ensure_account(
    session: aiohttp.ClientSession,
    api_key: str,
    school_name: str,
    phone: str,
    website: str,
    email: str = "",
) -> str | None:
    """Find or create the Apollo Account (org) for the school. Returns its id.

    Matches by DOMAIN, not name: a generic name like "Lincoln Elementary School"
    exists for many different schools, so reusing an account by name links the
    contact to the wrong school. We only reuse an existing account when its
    domain matches the school's domain (from the enriched website, else the
    captured email). Otherwise we create a fresh account carrying that domain so
    future calls to the same school match it.
    """
    if not school_name:
        return None

    # Best domain we have for this school: enriched website first, else the
    # captured contact email's domain (e.g. delmarsd.ca.us).
    domain = _domain_of_url(website) or _domain_of_email(email)

    if domain:
        try:
            async with session.post(
                f"{APOLLO_BASE_URL}/accounts/search",
                headers=_headers(api_key),
                json={"q_organization_name": school_name, "per_page": 25},
            ) as resp:
                if resp.status in (200, 201):
                    for acct in (await resp.json()).get("accounts", []):
                        acct_domain = (
                            acct.get("primary_domain")
                            or acct.get("domain")
                            or _domain_of_url(acct.get("website_url", ""))
                        ).lower()
                        if acct_domain == domain:
                            return acct.get("id")
        except Exception as e:
            logger.warning(f"Apollo account search error: {e}")

    # No domain match (or no domain to match on) — create a new account rather
    # than risk linking to a same-named but different school.
    payload = {"name": school_name, "phone": phone or "", "domain": domain}
    payload = {k: v for k, v in payload.items() if v}
    try:
        async with session.post(
            f"{APOLLO_BASE_URL}/accounts", headers=_headers(api_key), json=payload
        ) as resp:
            if resp.status in (200, 201):
                return ((await resp.json()).get("account") or {}).get("id")
            logger.warning(f"Apollo account create failed ({resp.status}): {await resp.text()}")
    except Exception as e:
        logger.warning(f"Apollo account create error: {e}")
    return None


async def enroll_security_contact(row: dict[str, str]) -> None:
    """Verify the captured contact against Apollo + the school site, then enroll.

    Best effort: logs and returns on any problem, never raises. No-op unless
    APOLLO_API_KEY is set and the row carries a captured contact. Mutates the
    row in place to record the verified email/phone and a validation note, so
    the server's results log reflects what was checked.
    """
    if not os.getenv("APOLLO_API_KEY"):
        logger.debug("Apollo enrollment skipped: APOLLO_API_KEY not set")
        return

    if row.get("outcome") != "contact_captured":
        return

    if not row.get("contact_email") and not row.get("contact_phone"):
        logger.warning("Apollo enrollment skipped: contact has no email or phone")
        return

    api_key = os.getenv("APOLLO_API_KEY")
    name = row.get("contact_name", "(unknown)")
    try:
        async with aiohttp.ClientSession() as session:
            person = await _match_person(session, api_key, row)
            verified = _verify(row, person)

            # Record what we verified back onto the outcome row.
            row["contact_email"] = verified["email"] or row.get("contact_email", "")
            row["contact_phone"] = verified["phone"] or row.get("contact_phone", "")
            row["verification"] = verified["note"]
            existing_notes = row.get("notes", "")
            row["notes"] = f"{existing_notes} | {verified['note']}".strip(" |")
            logger.info(f"Apollo: {name} — {verified['note']}")

            # Only create/track the school as an Apollo Account once a meeting
            # is set (Hailey captured a good time for a senior rep to follow up).
            account_id = None
            if row.get("contact_best_time"):
                account_id = await _ensure_account(
                    session,
                    api_key,
                    row.get("lead_company", ""),
                    row.get("lead_phone", ""),
                    verified.get("school_website", ""),
                    verified.get("email") or row.get("contact_email", ""),
                )
                if account_id:
                    logger.info(
                        f"Apollo: account tracked for {row.get('lead_company', '')} "
                        f"(meeting {row['contact_best_time']}) [{account_id}]"
                    )

            contact_id = await _create_or_update_contact(
                session, api_key, row, verified, account_id
            )
            if not contact_id:
                return
            added = await _add_to_sequence(
                session, api_key, contact_id, has_email=bool(verified["email"])
            )
            if added:
                logger.info(
                    f"Apollo: enrolled {name} ({row.get('contact_role', '')}) "
                    f"from {row.get('lead_company', '')} [contact {contact_id}]"
                )
    except Exception as e:
        logger.warning(f"Apollo enrollment error for {name}: {e}")
