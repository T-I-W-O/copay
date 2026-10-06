from .models import SiteInfo
from .models import UserProfile
from .models import Notification


def unread_notifications(request):
    if not request.user.is_authenticated:
        return {
            "unread_notification_count": 0,
        }

    return {
        "unread_notification_count": Notification.objects.filter(
            user=request.user,
            is_read=False
        ).count()
    }


def bank_profile(request):
    if not request.user.is_authenticated:
        return {"show_bank_modal": False}

    try:
        profile = request.user.profile

        incomplete = (
            not profile.bank
            or not profile.account_number
            or not profile.account_name
            or not profile.bank_verified
        )

        return {
            "show_bank_modal": incomplete,
        }

    except UserProfile.DoesNotExist:
        return {
            "show_bank_modal": True,
        }


def site_info(request):
    info = SiteInfo.objects.first()

    return {
        "site_info": info,
    }