
from django.http import HttpResponse
from django.shortcuts import redirect
from django.contrib.auth.decorators import user_passes_test, login_required


from django.http import HttpResponse
from django.urls import reverse
from django.http import Http404
from django.shortcuts import redirect
from django.contrib import messages
# core/decorators.py
from functools import wraps
from django.contrib import messages
from django.shortcuts import redirect

from .models import SiteInfo

REQUIRED_SITE_INFO_FIELDS = [
    "email",
    "phone",
    "bank",
    "bank_code",
    "account_number",
    "account_name",
]


def require_complete_site_info(view_func):
    """
    Blocks access to a view until SiteInfo has every required field filled
    in. Redirects to the settings page with an error message, where the
    empty sections will already render in edit mode.

    Do NOT put this on the settings view itself — that would redirect it
    to itself in an infinite loop.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        site_info = SiteInfo.objects.first()

        if site_info is None:
            messages.error(request, "Site settings are incomplete. Please fill in all required fields before continuing.")
            return redirect("settings")

        for field in REQUIRED_SITE_INFO_FIELDS:
            if not getattr(site_info, field):
                messages.error(request, "Site settings are incomplete. Please fill in all required fields before continuing.")
                return redirect("settings")

        return view_func(request, *args, **kwargs)

    return wrapper


def admin_only(view_func):
    def wrapper_func(request, *args, **kwargs):
        group = 'admin'  # Set the desired group name
        user_groups = request.user.groups.all()

        for user_group in user_groups:
            if user_group.name == 'customer':
                return redirect('dashboard')
            if user_group.name == 'admin':
                return view_func(request, *args, **kwargs)

        # If the user doesn't belong to any group or none of the groups match
        return redirect('dashboard')

    return wrapper_func


def customer_only(view_func):
    def wrapper_func(request, *args, **kwargs):
        group = 'customer'  # Set the desired group name
        user_groups = request.user.groups.all()

        for user_group in user_groups:
            if user_group.name == 'admin':
                return redirect('admin_page')
            if user_group.name == 'customer':
                return view_func(request, *args, **kwargs)

        # If the user doesn't belong to any group or none of the groups match
        return redirect('dashboard')

    return wrapper_func
