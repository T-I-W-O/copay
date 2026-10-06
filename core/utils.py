# utils.py

import os
import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.db.models import Avg, Count, Q
from decimal import Decimal
from .models import *
from decimal import Decimal
from datetime import timedelta

from django.utils import timezone


from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from .models import *

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

# Import your own models here, e.g.:
# from .models import Pool, PoolMember, PoolEarning, Payment, Refund

logger = logging.getLogger(__name__)

TWO_PLACES = Decimal("0.01")


def debug_print(*args):
    """Prints only when DEBUG=True, so nothing leaks in production."""
    if settings.DEBUG:
        print(*args)


   


import logging
from decimal import Decimal
from urllib.parse import quote

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q

# Adjust this import to wherever your models live.
from .models import Notification, Payment, PaymentHistory, Pool

import logging
from decimal import Decimal
from urllib.parse import quote

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q

# Adjust this import to wherever your models live.
from .models import Dispute, Notification, Payment, PaymentHistory, Pool, PoolMember

logger = logging.getLogger(__name__)

TWO_PLACES = Decimal("0.01")

PAYSCROW_APPLYCODE_URL = (
    "https://api.payscrow.net/api/v3/escrow/escrowtransactions/applycode"
)
PAYSCROW_RAISE_DISPUTE_URL = (
    "https://api.payscrow.net/api/v3/marketplace/transactions/{number}/broker/raise-dispute"
)

# Sent to Payscrow as the dispute reason (cancelled pools only)
REASON_CANCELLED = (
    "The pool was cancelled before the full service period was delivered. "
    "A partial refund is needed for the unused portion of the subscription; "
    "the host should only be paid for the days the service was provided."
)
REASON_REMOVED = (
    "The member was removed from the pool by CoPay administration after a "
    "dispute, before the full service period was delivered. A partial refund "
    "is requested for the unused portion of the subscription; the host should "
    "only be paid for the days the member had access."
)


# Who Payscrow should show as the person raising the dispute.
# Override with PAYSCROW_DISPUTE_REQUESTED_BY in settings.py if needed.
DISPUTE_REQUESTED_BY = getattr(settings, "PAYSCROW_DISPUTE_REQUESTED_BY", "customer")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fmt_date(value):
    return value.strftime("%d %b %Y") if value else "unknown"


def _build_complaint(payment, reason):
    """The complaint text sent to Payscrow: the reason plus this member's details."""
    member = payment.pool_member
    pool = member.pool
    joined = payment.stint_started_at or member.joined_at
    stopped = member.left_at or pool.end_date

    return (
        f"{reason}\n\n"
        f"Pool: {pool.service.name} (#{pool.pk}), host @{pool.host.username}\n"
        f"Member: @{member.user.username}\n"
        f"Transaction: {payment.reference}\n"
        f"Amount paid (host share): NGN {payment.amount}; CoPay fee: NGN {payment.copay_fees or 0}\n"
        f"Member joined: {_fmt_date(joined)}\n"
        f"Access ended: {_fmt_date(stopped)}"
    )


def _say(payment, message):
    """One print per step, tagged with who/what it is about."""
    print(f"[PAYOUT] {payment.pool_member.user.username} | {payment.reference} | {message}")


def _headers():
    return {
        "BrokerApiKey": settings.PAYSCROW_API_KEY,
        "Content-Type": "application/json",
    }


def _error_message(data):
    """Reads errors from the top level AND from the nested data object."""
    message = data.get("message") or "PayScrow rejected the transaction."

    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    errors = data.get("errors") or inner.get("errors")

    if errors:
        if isinstance(errors, list):
            message = " | ".join(str(e) for e in errors)
        else:
            message = str(errors)

    return message


def _payscrow_post(url, payload):
    """Returns (ok, error_message). Prints the request (code hidden) and the response."""
    safe_payload = {k: ("***" if k == "code" else v) for k, v in payload.items()}
    print("PAYSCROW REQUEST:", url, safe_payload)

    try:
        response = requests.post(url, json=payload, headers=_headers(), timeout=30)
    except requests.RequestException as e:
        print("PAYSCROW ERROR:", e)
        return False, "Could not connect to PayScrow."

    try:
        data = response.json()
    except ValueError:
        data = {}

    print("PAYSCROW RESPONSE:", response.status_code, data)

    # PayScrow returns HTTP 200 even when the real operation failed,
    # so the nested "data" object must be checked too.
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    inner_ok = (
        inner.get("success", True) is not False
        and inner.get("isSuccessful", True) is not False
        and not inner.get("errors")
    )

    if response.ok and data.get("success") and inner_ok:
        return True, ""

    return False, _error_message(data)


def _paid_payments(**filters):
    """
    Payments that count as paid: Payment.status is PAID, or the pool member is
    marked PAID. Failed and refunded payments are left out.
    """
    return (
        Payment.objects
        .filter(**filters)
        .filter(
            Q(status=Payment.Status.PAID)
            | Q(pool_member__payment_status=PoolMember.PaymentStatus.PAID)
        )
        .exclude(status__in=[Payment.Status.FAILED, Payment.Status.REFUNDED])
        .select_related("pool_member__user")
        .order_by("id")
    )


def _print_pool_members(pool):
    """Prints every member of the pool and their payments, so you can see exactly
    why a dispute was or was not created."""
    print(f"[PAYOUT] members of pool #{pool.pk}:")

    members = pool.members.select_related("user").all()

    if not members:
        print("[PAYOUT]   (no members at all)")

    for member in members:
        print(
            f"[PAYOUT]   - {member.user.username}: active={member.active} "
            f"member_payment_status={member.payment_status}"
        )

        payments = Payment.objects.filter(pool_member=member)

        if not payments:
            print("[PAYOUT]       no Payment rows for this member")

        for payment in payments:
            print(
                f"[PAYOUT]       payment status={payment.status} ref={payment.reference} "
                f"guid={'yes' if payment.transaction_guid else 'NO'} "
                f"dispute_raised={payment.payscrow_dispute_raised}"
            )


def _has_open_dispute(member):
    """
    True only if this member is involved in a dispute in this pool that is
    STILL OPEN (they opened it, or it was opened against them).
    Resolved disputes do not count.

    To block the WHOLE pool whenever any dispute is open instead, remove the
    .filter(Q(...)) line.
    """
    return (
        Dispute.objects
        .filter(pool=member.pool, status=Dispute.Status.OPEN)
        .filter(Q(opened_by=member.user) | Q(against_user=member.user))
        .exists()
    )


def _host_share(payment):
    """ASSUMPTION: payment.amount is the host share and copay_fees is separate.
    If payment.amount already includes copay_fees, subtract them here."""
    return max(payment.amount, Decimal("0.00")).quantize(TWO_PLACES)


def _notify_host(pool, amount):
    try:
        Notification.objects.create(
            user=pool.host,
            pool=pool,
            type=Notification.Type.PAYMENT,
            title="Payout Initiated",
            description=(
                f"Payment of ₦{amount} from your {pool.service.name} Shared "
                f"Pool has been initiated. It usually takes about 24 hours "
                f"to reach your bank."
            ),
            url=f"/market/{pool.pk}/",
        )
    except Exception:
        logger.exception("Could not create host notification")
        print("[PAYOUT] could not create host notification (see logs)")


# ---------------------------------------------------------------------------
# Cancelled pools: raise a Payscrow dispute, stop there
# ---------------------------------------------------------------------------
def _raise_dispute(payment, reason, dry_run=False):
    # Payscrow wants the transaction NUMBER (the MKT-... value, which is what
    # we save as payment.reference), not the GUID.
    number = payment.reference or payment.transaction_guid

    if not number:
        _say(payment, "no transaction number in the database, cannot create dispute")
        _say(payment, "no payment history is created")
        return "skipped_no_number", False

    if payment.payscrow_dispute_raised:
        _say(payment, "payscrow dispute already created")
        _say(payment, "no payment history is created")
        return "dispute_already_raised", True

    if dry_run:
        _say(payment, "DRY RUN: would create payscrow dispute")
        _say(payment, "no payment history is created")
        return "would_raise_dispute", True

    with transaction.atomic():
        locked = Payment.objects.select_for_update().get(pk=payment.pk)

        if locked.payscrow_dispute_raised:
            _say(payment, "payscrow dispute already created")
            _say(payment, "no payment history is created")
            return "dispute_already_raised", True

        # Payscrow's raise-dispute command requires: requestedBy, complaint
        ok, error = _payscrow_post(
            PAYSCROW_RAISE_DISPUTE_URL.format(number=quote(str(number), safe="")),
            {
                "requestedBy": DISPUTE_REQUESTED_BY,
                "complaint": _build_complaint(locked, reason),
            },
        )

        if not ok:
            _say(payment, f"payscrow dispute FAILED: {error}")
            _say(payment, "no payment history is created")
            return "dispute_failed", False

        locked.payscrow_dispute_raised = True
        locked.save(update_fields=["payscrow_dispute_raised"])

    _say(payment, "payscrow dispute created")
    _say(payment, "no payment history is created")
    return "dispute_raised", True


def raise_disputes_for_member(member, reason=REASON_REMOVED, dry_run=False):
    """
    Used when an admin removes ONE member: raises a Payscrow dispute for each
    of that member's paid payments that has not been paid out yet.
    Returns a list of result dicts (one per payment).
    """
    results = []

    payments = _paid_payments(pool_member=member)

    if not payments:
        print(f"[PAYOUT] {member.user.username} has no paid payments, no dispute to create")

    for payment in payments:
        entry = {
            "pool_member_id": member.pk,
            "username": member.user.username,
            "payment_reference": payment.reference,
            "action": None,
            "ok": False,
        }

        try:
            if PaymentHistory.objects.filter(
                payment=payment, kind=PaymentHistory.Kind.EARNING
            ).exists():
                _say(payment, "payment history already exists, host already paid, ignoring")
                _say(payment, "no dispute created")
                action, ok = "ignored_already_paid", True
            else:
                _say(payment, "member removed by admin")
                _say(payment, "creating payscrow dispute")
                action, ok = _raise_dispute(payment, reason, dry_run)

            entry.update(action=action, ok=ok)

        except Exception as e:
            logger.exception("Dispute failed for payment %s", payment.pk)
            _say(payment, f"ERROR: {e}")
            entry.update(action="error", ok=False, error=str(e))

        results.append(entry)

    return results


# ---------------------------------------------------------------------------
# Successful pools with no open dispute: release funds to the host
# ---------------------------------------------------------------------------
def _pay_host(payment, pool, dry_run=False):
    # Missing values are only printed, never raised as an error
    missing = []
    if not payment.transaction_guid:
        missing.append("transaction guid")
    if not payment.ecrow_code:
        missing.append("escrow code")

    if missing:
        _say(payment, f"no {' or '.join(missing)} in the database")
        _say(payment, "no payment history is created")
        return "skipped_missing_code", False

    if dry_run:
        _say(payment, "DRY RUN: would apply escrow code to pay host")
        return "would_pay_host", True

    with transaction.atomic():
        locked = Payment.objects.select_for_update().get(pk=payment.pk)

        # Never apply the code twice
        if PaymentHistory.objects.filter(
            payment=locked, kind=PaymentHistory.Kind.EARNING
        ).exists():
            _say(payment, "host already paid (payment history exists), skipping")
            return "already_paid", True

        # Same request as release_escrow_view
        ok, error = _payscrow_post(
            PAYSCROW_APPLYCODE_URL,
            {
                "transactionId": locked.transaction_guid,
                "code": locked.ecrow_code,
            },
        )

        if not ok:
            _say(payment, f"payscrow rejected the request: {error}")
            _say(payment, "(the escrow code or transaction guid saved in the database may be wrong)")
            _say(payment, "no payment history is created")
            return "pay_failed", False

        amount = _host_share(locked)

        PaymentHistory.objects.create(
            user=pool.host,
            pool=pool,
            payment=locked,
            kind=PaymentHistory.Kind.EARNING,
            amount=amount,
            payment_reference=(locked.reference or "")[:100],
        )

    _say(payment, "payscrow accepted, funds requested for the host")
    _say(payment, f"payment history created (₦{amount})")

    # Notification for a successful pool with no open dispute
    _notify_host(pool, amount)
    _say(payment, "host notified")

    return "paid_host", True


# ---------------------------------------------------------------------------
# Cancelled pool: raise the disputes ONCE, the moment the pool is cancelled
# (the signal calls this). Nothing else in this file raises disputes.
# ---------------------------------------------------------------------------
def raise_disputes_for_pool(pool, reason=REASON_CANCELLED, dry_run=False):
    """
    Raises a Payscrow dispute for every paid payment in a cancelled pool that
    has not been paid out. A payment that already has a dispute is skipped,
    so calling this twice never creates a second dispute.
    Returns a list of result dicts (one per payment).
    """
    print(f"\n========== CANCELLED POOL #{pool.pk} ({pool.service.name}): RAISING DISPUTES ==========")

    results = []

    _print_pool_members(pool)

    payments = _paid_payments(pool_member__pool=pool)

    print(f"[PAYOUT] paid payments found for this pool: {len(payments)}")

    if not payments:
        print("[PAYOUT] no paid payments in this pool, no dispute to create")

    for payment in payments:
        member = payment.pool_member

        entry = {
            "pool_id": pool.pk,
            "pool_member_id": member.pk,
            "username": member.user.username,
            "payment_reference": payment.reference,
            "action": None,
            "ok": False,
        }

        try:
            if PaymentHistory.objects.filter(
                payment=payment, kind=PaymentHistory.Kind.EARNING
            ).exists():
                _say(payment, "payment history already exists, host already paid, ignoring")
                _say(payment, "no dispute created")
                action, ok = "ignored_already_paid", True
            else:
                _say(payment, "pool cancelled")
                _say(payment, "creating payscrow dispute")
                action, ok = _raise_dispute(payment, reason, dry_run)

            entry.update(action=action, ok=ok)

        except Exception as e:
            logger.exception("Dispute failed for payment %s", payment.pk)
            _say(payment, f"ERROR: {e}")
            entry.update(action="error", ok=False, error=str(e))

        results.append(entry)

    return results


# ---------------------------------------------------------------------------
# Main util: SUCCESSFUL (completed) pools only. Never creates a dispute.
# ---------------------------------------------------------------------------
def process_pool_payouts(pool=None, dry_run=False):
    """
    For every COMPLETED pool, go through each PAID payment:

      - Already has a PaymentHistory          -> ignore (already paid out)
      - Payscrow dispute already raised       -> ignore (the arbiter decides)
      - Member has an OPEN platform dispute   -> ignore (no payout, no history)
      - Otherwise (resolved disputes are fine)-> apply the escrow code to
                                                 release funds, create
                                                 PaymentHistory, notify host

    Cancelled pools are IGNORED here: their disputes are raised once, at the
    moment of cancellation (raise_disputes_for_pool), and when an admin removes
    a member (raise_disputes_for_member).

    Missing / wrong transaction guid or escrow code is only printed.
    Safe to run repeatedly. dry_run=True prints what would happen without
    calling Payscrow or saving anything.
    """

    pools = (
        Pool.objects
        .filter(status=Pool.Status.COMPLETED)
        .select_related("host", "service")
    )

    if pool:
        pools = pools.filter(pk=pool.pk)

    results = []

    for p in pools:
        print(f"\n========== PAYOUT: POOL #{p.id} ({p.service.name}) status={p.status} ==========")

        payments = _paid_payments(pool_member__pool=p)

        if not payments:
            print("[PAYOUT] no paid payments in this pool, nothing to do")

        for payment in payments:
            member = payment.pool_member

            entry = {
                "pool_id": p.pk,
                "pool_member_id": member.pk,
                "username": member.user.username,
                "payment_reference": payment.reference,
                "action": None,
                "ok": False,
            }

            try:
                # Already paid out? Ignore completely so it can never pay twice.
                if PaymentHistory.objects.filter(
                    payment=payment, kind=PaymentHistory.Kind.EARNING
                ).exists():
                    _say(payment, "payment history already exists, host already paid, ignoring")
                    _say(payment, "no payment requested")
                    action, ok = "ignored_already_paid", True

                # A Payscrow dispute is already open for this payment (the
                # member was removed): the arbiter decides, never release it.
                elif payment.payscrow_dispute_raised:
                    _say(payment, "payscrow dispute already raised, waiting for the ruling, ignoring")
                    _say(payment, "no payment requested, no payment history is created")
                    action, ok = "ignored_payscrow_dispute", True

                else:
                    _say(payment, "pool successful")

                    if _has_open_dispute(member):
                        _say(payment, "member has an OPEN dispute, ignoring")
                        _say(payment, "no payment requested, no payment history is created")
                        action, ok = "ignored_open_dispute", True
                    else:
                        _say(payment, "no open dispute (resolved ones are fine)")
                        _say(payment, "requesting funds to the host")
                        action, ok = _pay_host(payment, p, dry_run)

                entry.update(action=action, ok=ok)

            except Exception as e:
                logger.exception("Payout failed for payment %s", payment.pk)
                _say(payment, f"ERROR: {e}")
                entry.update(action="error", ok=False, error=str(e))

            results.append(entry)

    return results

#---------------------------------------------------------------- 


def auto_confirm_access(pool):
    now = timezone.now()
    deadline = now - timedelta(hours=24)

    members = PoolMember.objects.filter(
        pool=pool,
        payment_status=PoolMember.PaymentStatus.PAID,
        confirmed_access=False,
        dispute=False,
        joined_at__lte=deadline,
    )

    for member in members:
        has_open_dispute = Dispute.objects.filter(
            pool=pool,
            status=Dispute.Status.OPEN,
            against_user=member.user,
        ).exists()

        if not has_open_dispute:
            member.confirmed_access = True
            member.confirmed_at = now
            member.save(update_fields=[
                "confirmed_access",
                "confirmed_at",
            ])


def get_pool_price_without_site_fee(pool):
    """
    Removes the site's percentage from Pool.price.

    Example:
        Pool.price = ₦10,500
        Site percentage = 5%

        Actual pool price = ₦10,500 / 1.05
                         = ₦10,000
    """

    site_info = SiteInfo.objects.first()

    if not site_info or site_info.site_percentage == Decimal("0.00"):
        return pool.price.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP
        )

    percentage = site_info.site_percentage / Decimal("100")

    actual_price = pool.price / (Decimal("1.00") + percentage)

    return actual_price.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )


def calculate_reputation(user):
    # ---------------------------------------------------------
    # 1. RATING
    # Only ratings from pools that were NOT cancelled
    # ---------------------------------------------------------
    ratings_qs = Rating.objects.filter(
        host=user
    ).exclude(
        pool__status=Pool.Status.CANCELLED
    )

    total_ratings = ratings_qs.count()

    if total_ratings > 0:
        avg_rating = ratings_qs.aggregate(
            avg=Avg("rating")
        )["avg"]

        rating_value = round(
            Decimal(str(avg_rating)),
            2
        )
    else:
        rating_value = Decimal("0.00")

    # ---------------------------------------------------------
    # 2. SUCCESSFUL POOLS
    # Stored in Reputation
    # ---------------------------------------------------------
    rep, created = Reputation.objects.get_or_create(
        user=user
    )

    successful_pools_count = rep.successful_pools

    # ---------------------------------------------------------
    # 3. TOTAL POOLS CREATED BY THIS USER AS HOST
    # ---------------------------------------------------------
    total_pools = user.hosted_pools.count()

    # ---------------------------------------------------------
    # 4. SUCCESS RATE
    #
    # Successful pools / all pools hosted by user
    # ---------------------------------------------------------
    if total_pools > 0:
        success_rate = int(
            (successful_pools_count / total_pools) * 100
        )
    else:
        success_rate = 0

    # ---------------------------------------------------------
    # 5. RESOLVED DISPUTES
    #
    # Only disputes AGAINST the user.
    # If the user opened the dispute themselves,
    # it does not count.
    # ---------------------------------------------------------
    resolved_disputes_count = Dispute.objects.filter(
        against_user=user,
        status=Dispute.Status.RESOLVED
    ).exclude(
        opened_by=user
    ).count()

    # ---------------------------------------------------------
    # 6. UNRESOLVED DISPUTES
    # ---------------------------------------------------------
    unresolved_disputes_count = Dispute.objects.filter(
        against_user=user,
        status=Dispute.Status.OPEN
    ).exclude(
        opened_by=user
    ).count()

    # ---------------------------------------------------------
    # 7. UPDATE STORED REPUTATION DATA
    # ---------------------------------------------------------
    rep.save()

    return {
        "rating": rating_value,
        "total_ratings": total_ratings,
        "success_rate": success_rate,
        "successful_pools": successful_pools_count,
        "resolved_disputes": resolved_disputes_count,
        "unresolved_disputes": unresolved_disputes_count,
    }


def encrypt_account_number(account_number):
    key = base64.urlsafe_b64decode(settings.ACCOUNT_ENCRYPTION_KEY)

    aesgcm = AESGCM(key)

    # AES-GCM needs a unique nonce for every encryption
    nonce = os.urandom(12)

    encrypted = aesgcm.encrypt(
        nonce,
        account_number.encode(),
        None
    )

    # Store nonce + encrypted data together
    return base64.urlsafe_b64encode(
        nonce + encrypted
    ).decode()


def decrypt_account_number(encrypted_account):
    key = base64.urlsafe_b64decode(settings.ACCOUNT_ENCRYPTION_KEY)

    data = base64.urlsafe_b64decode(encrypted_account)

    nonce = data[:12]
    encrypted = data[12:]

    aesgcm = AESGCM(key)

    return aesgcm.decrypt(
        nonce,
        encrypted,
        None
    ).decode()
    
    
