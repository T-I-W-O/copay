import logging
import threading
from contextlib import contextmanager
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, F, Q
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .models import Dispute, Notification, Pool, PoolMember, Reputation
from .utils import *  # process_pool_payouts, raise_disputes_for_pool

logger = logging.getLogger(__name__)


# =========================================================
# Re-entry guard: saves we make ourselves (closing a pool,
# completing it, setting active=False) must not trigger the
# signal again.
# =========================================================
_state = threading.local()


@contextmanager
def _no_resync():
    previous = getattr(_state, "busy", False)
    _state.busy = True
    try:
        yield
    finally:
        _state.busy = previous


# =========================================================
# Small helpers
# =========================================================
def _notify(user, pool, notif_type, title, description):
    """Creates one notification. A failure is printed, never raised."""
    try:
        Notification.objects.create(
            user=user,
            pool=pool,
            type=notif_type,
            title=title,
            description=description,
            url=f"/market/{pool.pk}/",
        )
        print(f"[SIGNAL]    notified {user.username}: {title}")
    except Exception:
        logger.exception("Could not create notification '%s' for %s", title, user)
        print(f"[SIGNAL] !! could not notify {user.username}: {title}")


def _unique_users(members):
    """One entry per user (a user can have several PoolMember rows)."""
    users = {}
    for member in members:
        users[member.user_id] = member.user
    return list(users.values())


def update_host_reputation(host):
    """
    successful_pools = number of COMPLETED pools this user HOSTED
    (not pools they joined). Recounted every time, so it can never drift.
    """
    count = Pool.objects.filter(host=host, status=Pool.Status.COMPLETED).count()

    reputation, _ = Reputation.objects.get_or_create(user=host)
    reputation.successful_pools = count
    reputation.save(update_fields=["successful_pools"])

    print(f"[SIGNAL] -> Reputation: {host.username} has hosted {count} successful pool(s).")


def _send_rating_reminders(pool, users, text):
    for user in users:
        if pool.ratings.filter(reviewer=user).exists():
            print(f"[SIGNAL]    {user.username} already rated pool #{pool.pk}, no reminder")
            continue

        _notify(user, pool, Notification.Type.RATING, "Rate Your Pool", text)


# =========================================================
# CHECK 1: close every pool that has reached max users
# =========================================================
def close_full_pools():
    full_pools = (
        Pool.objects
        .filter(active=True, status=Pool.Status.OPEN)
        .annotate(active_count=Count("members", filter=Q(members__active=True)))
        .filter(active_count__gte=F("number_of_users"))
    )

    for pool in full_pools:
        print(f"[SIGNAL] Pool #{pool.pk} reached max users ({pool.active_count}/{pool.number_of_users}). Closing pool.")
        pool.status = Pool.Status.CLOSE
        pool.save(update_fields=["status"])


# =========================================================
# CHECK 2: complete every expired pool
# =========================================================
def complete_expired_pool(pool):
    """
    Marks an expired pool COMPLETED with end_date = the day it was supposed
    to end, then schedules finalize_completed_pool() after the commit.

    - A pool that already has an end_date is treated as cancelled/ended
      early and is never converted to COMPLETED.
    - The pool row is locked, so it can't be completed twice.

    Returns True if THIS call completed the pool.
    """
    now = timezone.now()
    expiry_date = pool.created_at + timedelta(days=pool.duration_days)

    with transaction.atomic():
        locked = Pool.objects.select_for_update().get(pk=pool.pk)

        if locked.end_date is not None:
            print(f"[SIGNAL] Pool #{locked.pk} has an end_date, not converting to COMPLETED.")
            return False

        if locked.status not in [Pool.Status.OPEN, Pool.Status.CLOSE]:
            return False

        if now < expiry_date:
            return False

        print(f"[SIGNAL] -> Pool #{locked.pk} has expired! Converting to COMPLETED.")

        locked.status = Pool.Status.COMPLETED
        locked.end_date = expiry_date
        locked.active = False
        locked.save(update_fields=["status", "end_date", "active"])

    transaction.on_commit(lambda: finalize_completed_pool(locked.pk))
    return True


def complete_all_expired_pools():
    completed = 0

    pools = Pool.objects.filter(
        active=True,
        end_date__isnull=True,
        status__in=[Pool.Status.OPEN, Pool.Status.CLOSE],
    )

    for pool in pools:
        if complete_expired_pool(pool):
            completed += 1

    return completed


# =========================================================
# Check everything
# =========================================================
def sync_all_pools():
    """Looks at EVERY pool, whichever pool or member triggered this."""
    with _no_resync():
        close_full_pools()
        completed = complete_all_expired_pools()
        print(f"[SIGNAL] Sync finished. Pools completed this run: {completed}")


# =========================================================
# SUCCESSFUL pool: everything that happens after it completes
# =========================================================
def finalize_completed_pool(pool_id):
    with _no_resync():
        pool = Pool.objects.select_related("host", "service").get(pk=pool_id)

        print(f"\n[SIGNAL] ========== POOL #{pool.pk} COMPLETED ({pool.service.name}) ==========")

        # -------------------------------------------------
        # A. Pay the host. The util ignores members with an OPEN dispute and
        #    payments that were already paid out.
        # -------------------------------------------------
        print(f"[SIGNAL] -> Running payouts for pool #{pool.pk}.")
        try:
            results = process_pool_payouts(pool=pool)
            print(f"[SIGNAL] -> Payout results: {results}")
        except Exception:
            logger.exception("Payout run failed for pool %s", pool.pk)
            print(f"[SIGNAL] !! Payout run FAILED for pool #{pool.pk}. It is safe to run again.")

        # -------------------------------------------------
        # B. Notifications
        # -------------------------------------------------
        print(f"[SIGNAL] -> Sending notifications for pool #{pool.pk}.")

        members = pool.members.filter(active=True).select_related("user")
        member_users = _unique_users(members)

        # Everyone: the pool is completed
        _notify(
            pool.host, pool, Notification.Type.POOL, "Pool Completed",
            f"Your {pool.service.name} Shared Pool has successfully completed.",
        )
        for user in member_users:
            _notify(
                user, pool, Notification.Type.POOL, "Pool Completed",
                f"The {pool.service.name} Shared Pool has successfully completed.",
            )

        # Host: change credentials
        _notify(
            pool.host, pool, Notification.Type.SECURITY, "Update Your Service Credentials",
            f"Your {pool.service.name} Shared Pool has ended. Other pool members "
            f"may still have access to the service, so please change your "
            f"password and any shared credentials to secure your account.",
        )

        # Members who haven't rated: reminder
        _send_rating_reminders(
            pool, member_users,
            f"Your {pool.service.name} Shared Pool has completed. "
            f"Please take a moment to rate your experience with the host.",
        )

        # -------------------------------------------------
        # C. Host reputation (pools HOSTED, not joined)
        # -------------------------------------------------
        update_host_reputation(pool.host)

        print(f"[SIGNAL] ========== POOL #{pool.pk} DONE ==========\n")


# =========================================================
# CANCELLED pool: runs once, at the moment a pool becomes CANCELLED
# =========================================================
def finalize_cancelled_pool(pool_id):
    with _no_resync():
        pool = Pool.objects.select_related("host", "service").get(pk=pool_id)

        print(f"\n[SIGNAL] ========== POOL #{pool.pk} CANCELLED ({pool.service.name}) ==========")

        # -------------------------------------------------
        # A. Raise Payscrow disputes for the paid members. This is the ONLY
        #    place disputes are raised for a cancelled pool (once, now).
        #    Payments already paid out or already disputed are skipped.
        # -------------------------------------------------
        print(f"[SIGNAL] -> Raising Payscrow disputes for cancelled pool #{pool.pk}.")
        results = []
        try:
            results = raise_disputes_for_pool(pool)
            print(f"[SIGNAL] -> Dispute results: {results}")
        except Exception:
            logger.exception("Raising disputes failed for cancelled pool %s", pool.pk)
            print(f"[SIGNAL] !! Raising disputes FAILED for cancelled pool #{pool.pk}.")

        # -------------------------------------------------
        # B. Notifications
        # -------------------------------------------------
        print(f"[SIGNAL] -> Sending notifications for cancelled pool #{pool.pk}.")

        members = pool.members.filter(
            Q(active=True) | Q(payment_status=PoolMember.PaymentStatus.PAID)
        ).select_related("user")
        member_users = _unique_users(members)

        _notify(
            pool.host, pool, Notification.Type.POOL, "Pool Cancelled",
            f"Your {pool.service.name} Shared Pool has been cancelled.",
        )
        # username -> True only if EVERY dispute for that user succeeded
        dispute_ok = {}
        for r in results:
            dispute_ok[r["username"]] = dispute_ok.get(r["username"], True) and bool(r.get("ok"))

        for user in member_users:
            if dispute_ok.get(user.username):
                text = (
                    f"The {pool.service.name} Shared Pool has been cancelled. "
                    f"A dispute has been opened with our payment partner to request "
                    f"a partial refund for the period you did not use."
                )
            elif user.username in dispute_ok:
                text = (
                    f"The {pool.service.name} Shared Pool has been cancelled. "
                    f"Our team is reviewing your refund and will contact you."
                )
            else:
                text = f"The {pool.service.name} Shared Pool has been cancelled."

            _notify(user, pool, Notification.Type.POOL, "Pool Cancelled", text)

        _notify(
            pool.host, pool, Notification.Type.SECURITY, "Update Your Service Credentials",
            f"Your {pool.service.name} Shared Pool has ended. Other pool members "
            f"may still have access to the service, so please change your "
            f"password and any shared credentials to secure your account.",
        )

        print(f"[SIGNAL] ========== CANCELLED POOL #{pool.pk} DONE ==========\n")


# =========================================================
# THE SIGNALS
# =========================================================
@receiver(pre_save, sender=Pool)
def remember_old_pool_status(sender, instance, **kwargs):
    """So the post_save can tell 'just became CANCELLED' from 'was already cancelled'."""
    if instance.pk:
        instance._old_status = (
            Pool.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
        )
    else:
        instance._old_status = None


@receiver(post_save, sender=Pool)
@receiver(post_save, sender=PoolMember)
def sync_pools_on_change(sender, instance=None, raw=False, **kwargs):
    if raw or getattr(_state, "busy", False):
        return

    print(f"[SIGNAL] {sender.__name__} #{instance.pk} saved -> checking ALL pools")

    # A pool that has JUST become cancelled
    if (
        sender is Pool
        and instance.status == Pool.Status.CANCELLED
        and getattr(instance, "_old_status", None) != Pool.Status.CANCELLED
    ):
        print(f"[SIGNAL] -> Pool #{instance.pk} was just cancelled.")
        transaction.on_commit(lambda: finalize_cancelled_pool(instance.pk))

    sync_all_pools()


# =========================================================
# DISPUTE RESOLVED: run the payouts again for that pool
# =========================================================
def rerun_payouts_after_dispute(pool_id, dispute_id):
    """
    A completed pool ignores members with an OPEN dispute. Once a dispute is
    resolved, run the payouts again so those members are paid now.
    The util skips payments already paid out and disputes already raised,
    so running it again is safe.
    """
    with _no_resync():
        pool = Pool.objects.select_related("host", "service").get(pk=pool_id)

        print(f"\n[SIGNAL] ========== DISPUTE #{dispute_id} RESOLVED (pool #{pool.pk}, status={pool.status}) ==========")

        if pool.status != Pool.Status.COMPLETED:
            print(f"[SIGNAL] -> Pool #{pool.pk} is not a completed pool, nothing to pay.")
            return

        print(f"[SIGNAL] -> Re-running payouts for pool #{pool.pk}.")
        try:
            results = process_pool_payouts(pool=pool)
            print(f"[SIGNAL] -> Payout results: {results}")
        except Exception:
            logger.exception("Payout re-run failed for pool %s after dispute %s", pool.pk, dispute_id)
            print(f"[SIGNAL] !! Payout re-run FAILED for pool #{pool.pk}. It is safe to run again.")

        print(f"[SIGNAL] ========== DISPUTE #{dispute_id} DONE ==========\n")


@receiver(pre_save, sender=Dispute)
def remember_old_dispute_status(sender, instance, **kwargs):
    """So post_save can tell 'just became RESOLVED' from 'was already resolved'."""
    if instance.pk:
        instance._old_status = (
            Dispute.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
        )
    else:
        instance._old_status = None


@receiver(post_save, sender=Dispute)
def payouts_when_dispute_resolved(sender, instance, raw=False, **kwargs):
    if raw:
        return

    if (
        instance.status == Dispute.Status.RESOLVED
        and getattr(instance, "_old_status", None) != Dispute.Status.RESOLVED
    ):
        print(f"[SIGNAL] Dispute #{instance.pk} was just resolved.")
        transaction.on_commit(
            lambda: rerun_payouts_after_dispute(instance.pool_id, instance.pk)
        )