import base64
import hashlib
import hmac
import json
import logging
import random
import re
import requests
import time
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import quote

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from django.conf import settings
from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import Group, User
from django.core.mail import EmailMultiAlternatives, send_mail
from django.core.paginator import Paginator
from django.db import models, transaction
from django.db.models import Count, DecimalField, Exists, F, OuterRef, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .decorators import *
from .models import *

from .utils import *

def cron_sync_pools(request):
    sync_all_pools()
    return JsonResponse({"ok": True})

@require_http_methods(["GET", "POST"])
def forget(request):
    if request.method == "GET":
        return render(request, "forget_password.html")

    # Handle AJAX POST requests
    import json

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse(
            {"status": "error", "message": "Invalid request data."}, status=400
        )

    action = data.get("action")

    # ----------------------------------------------------
    # STEP 1A: Send OTP
    # ----------------------------------------------------
    if action == "send_otp":
        email = data.get("email", "").strip().lower()
        if not email:
            return JsonResponse(
                {"status": "error", "message": "Email is required."}, status=400
            )

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return JsonResponse(
                {"status": "error", "message": "Email does not exist."},
                status=404,
            )

        # Generate a random 5-digit OTP
        otp_code = str(random.randint(10000, 99999))
        expires_at = timezone.now() + timedelta(minutes=10)

        # Create PasswordResetOTP record
        PasswordResetOTP.objects.create(
            user=user, otp=otp_code, expires_at=expires_at, is_used=False
        )

        # Send email using email.html template
        context = {
            "username": user.username,
            "xxxxx": otp_code,
        }
        html_content = render_to_string("email.html", context)
        text_content = strip_tags(html_content)

        subject = "Your Password Reset OTP Code"
        from_email = None  # Uses DEFAULT_FROM_EMAIL from settings.py

        try:
            email_message = EmailMultiAlternatives(
                subject, text_content, from_email, [email]
            )
            email_message.attach_alternative(html_content, "text/html")
            email_message.send(fail_silently=False)
        except Exception as e:
            return JsonResponse(
                {
                    "status": "error",
                    "message": "Failed to send email. Please try again later.",
                },
                status=500,
            )

        # Store email in session for validation in subsequent steps
        request.session["reset_email"] = email
        return JsonResponse(
            {
                "status": "success",
                "message": "OTP sent. This code is valid for 10 minutes.",
            }
        )

    # ----------------------------------------------------
    # STEP 1B: Verify OTP
    # ----------------------------------------------------
    elif action == "verify_otp":
        email = request.session.get("reset_email") or data.get(
            "email", ""
        ).strip().lower()
        otp_submitted = data.get("otp", "").strip()

        if not email or not otp_submitted:
            return JsonResponse(
                {
                    "status": "error",
                    "message": "Email and OTP are required.",
                },
                status=400,
            )

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return JsonResponse(
                {"status": "error", "message": "Invalid request session."},
                status=400,
            )

        # Get latest unused OTP for user
        latest_otp = (
            PasswordResetOTP.objects.filter(user=user, is_used=False)
            .order_by("-created_at")
            .first()
        )

        if not latest_otp or latest_otp.otp != otp_submitted:
            return JsonResponse(
                {
                    "status": "error",
                    "message": "The verification code is invalid.",
                },
                status=400,
            )

        if timezone.now() > latest_otp.expires_at:
            return JsonResponse(
                {
                    "status": "error",
                    "message": "This code has expired. Please request a new code.",
                },
                status=400,
            )

        # Mark session as verified for password reset stage
        request.session["otp_verified"] = True
        return JsonResponse(
            {
                "status": "success",
                "message": "OTP verified successfully.",
            }
        )

    # ----------------------------------------------------
    # STEP 2: Update Password
    # ----------------------------------------------------
    elif action == "update_password":
        if not request.session.get("otp_verified"):
            return JsonResponse(
                {
                    "status": "error",
                    "message": "Unauthorized step access. Please verify OTP first.",
                },
                status=403,
            )

        email = request.session.get("reset_email")
        new_password = data.get("new_password", "")
        confirm_password = data.get("confirm_password", "")

        if not new_password or not confirm_password:
            return JsonResponse(
                {
                    "status": "error",
                    "message": "Both password fields are required.",
                },
                status=400,
            )

        if new_password != confirm_password:
            return JsonResponse(
                {
                    "status": "error",
                    "message": "Passwords do not match.",
                },
                status=400,
            )

        # Password validations: at least 8 digits long with at least a number
        if len(new_password) < 8 or not any(
            char.isdigit() for char in new_password
        ):
            return JsonResponse(
                {
                    "status": "error",
                    "message": "Password must be at least 8 characters long and contain at least one number.",
                },
                status=400,
            )

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return JsonResponse(
                {"status": "error", "message": "User not found."}, status=404
            )

        # Update password using Django's hasher
        user.set_password(new_password)
        user.save()

        # Mark latest OTP as used
        latest_otp = (
            PasswordResetOTP.objects.filter(user=user, is_used=False)
            .order_by("-created_at")
            .first()
        )
        if latest_otp:
            latest_otp.is_used = True
            latest_otp.save()

        # Clear reset session data
        request.session.pop("reset_email", None)
        request.session.pop("otp_verified", None)

        return JsonResponse(
            {
                "status": "success",
                "message": "Your password has been updated successfully.",
            }
        )

    return JsonResponse(
        {"status": "error", "message": "Invalid action."}, status=400
    )


@login_required(login_url="signin")
def marketplace(request):
    selected_category = request.GET.get("category", "all")
    search_query = request.GET.get("q", "").strip()

    # Subquery to check if current user has an active membership in the pool
    user_membership = PoolMember.objects.filter(
        pool=OuterRef("pk"),
        user=request.user,
        active=True
    )

    pools = (
        Pool.objects.filter(
            status__in=[
                Pool.Status.OPEN,
                Pool.Status.CLOSE,
            ],
        )
        .filter(
            Q(is_public=True) | Q(host=request.user)
        )
        .select_related("host", "service")
        .annotate(
            contributor_count=Count(
                "members",
                filter=Q(members__role=PoolMember.Role.CONTRIBUTOR, members__active=True)
            ),
            user_is_member=Exists(user_membership)
        )
        .order_by("-created_at")
    )

    # Filter by category if specified and not 'all'
    if selected_category and selected_category != "all":
        pools = pools.filter(service__category=selected_category)

    # Filter by search query
    if search_query:
        pools = pools.filter(
            Q(service__name__icontains=search_query) | 
            Q(host__username__icontains=search_query)
        )

    paginator = Paginator(pools, 12)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    context = {
        "pools": page_obj,
        "selected_category": selected_category,
        "search_query": search_query,
    }
    return render(request, "marketplace.html", context)

def landing_page(request):
    services = Service.objects.all().order_by("name")
    
    cervices = (
        Service.objects
        .filter(type=Service.Type.STANDARD)
        .order_by("name")
    )
    
    active_pools = (
        Pool.objects
        .filter(
            
            
             status__in=[
                Pool.Status.OPEN,
                Pool.Status.CLOSE,
            ],
            is_public=True
        )
        .select_related("host", "service")
        .annotate(
            contributor_count=Count(
                "members",
                filter=Q(
                    members__role=PoolMember.Role.CONTRIBUTOR,
                    members__active=True
                )
            )
        )
        .order_by("-created_at")[:3]
    )
    market_pools = list(
        Pool.objects
        .filter(
            status__in=[
                Pool.Status.OPEN,
                Pool.Status.CLOSE,
            ],
            is_public=True
        )
        .select_related("host", "service")
        .annotate(
            contributor_count=Count(
                "members",
                filter=Q(
                    members__role=PoolMember.Role.CONTRIBUTOR,
                    members__active=True
                )
            )
        )
        .order_by("-created_at")[:8]
    )
    

    # Attach user_is_member flag safely to each pool
    if request.user.is_authenticated:
        user_member_pool_ids = set(
            PoolMember.objects.filter(
                user=request.user,
                active=True,
                pool__in=market_pools
            ).values_list('pool_id', flat=True)
        )
        for pool in market_pools:
            pool.user_is_member = pool.id in user_member_pool_ids
    else:
        for pool in market_pools:
            pool.user_is_member = False

    context = {
        "services": services,
        "cervices": cervices,
        "active_pools": active_pools,
        "market_pools": market_pools,
    }
    return render(request, "index.html", context)   

   

@login_required(login_url="signin")
@customer_only
def home(request):
    # 1. Run pool calculations

    host_reputation = calculate_reputation(request.user)


    recent_payments = PaymentHistory.objects.filter(user=request.user).order_by(
        "-created_at"
    )[:8]

    # 2. Fetch User Profile safely using standard related_name
    profile = getattr(request.user, 'profile', None)

    # 3. Decrypt and mask account number (showing only last 4 digits)
    masked_account_number = "••••••••"
    if profile and profile.account_number:
        try:
            decrypted_acc = decrypt_account_number(profile.account_number)
            if len(decrypted_acc) >= 4:
                masked_account_number = "••••" + decrypted_acc[-4:]
            else:
                masked_account_number = decrypted_acc
        except Exception:
            masked_account_number = "Invalid/Error"

    return render(
        request,
        "dashboard.html",
        {

            "recent_payments": recent_payments,
            "profile": profile,
            "masked_account_number": masked_account_number,
            "host_reputation": host_reputation,
        },
    )

def privacy(request):
    return render(request, "privacy.html")

def signin_view(request):
    if request.method == "POST":
        email = request.POST.get("email", "").strip()
        password = request.POST.get("password", "")

        try:
            user_obj = User.objects.get(email=email)
            username = user_obj.username
        except User.DoesNotExist:
            username = None

        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)

            if user.groups.filter(name="admin").exists():
                return redirect("admin_page")
            else:
                return redirect("marketplace")
        else:
            return render(
                request,
                "auth.html",
                {"error_message": "Invalid email or password. Please try again."},
            )

    return render(request, "auth.html")

def signup_view(request):
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        email = request.POST.get("email", "").strip()
        whatsapp = request.POST.get("whatsapp", "").strip()
        password = request.POST.get("password", "")
        consent = request.POST.get("consent")

        # 1. Check required fields
        if not username or not email or not whatsapp or not password:
            return render(
                request,
                "auth.html",
                {"error_message": "All fields are required.",
                 'active_tab': 'signup',
                 },
            )

        # 2. Email domain validation (@.com check)
        if not re.match(r"^[^@\s]+@[^@\s]+\.com$", email, re.IGNORECASE):
            return render(
                request,
                "auth.html",
                {
                    "error_message": (
                        "Invalid email format. Must contain '@' and end with a"
                        " '.com' domain."
                    ),
                    'active_tab': 'signup',
                },
            )

        # 3. WhatsApp number validation (234 followed by 10 digits)
        if not re.match(r"^234\d{10}$", whatsapp):
            return render(
                request,
                "auth.html",
                {
                    "error_message": (
                        "WhatsApp number must start with '234' followed by exactly 10"
                        " digits."
                    ),
                    'active_tab': 'signup',
                },
            )

        # 4. Password validation (at least 8 chars, mixed with numbers)
        if len(password) < 8 or not any(char.isdigit() for char in password):
            return render(
                request,
                "auth.html",
                {
                    "error_message": (
                        "Password must be at least 8 characters long and contain at"
                        " least one number."
                    ),
                    'active_tab': 'signup',
                },
            )

        # 5. Consent checkbox check
        if not consent:
            return render(
                request,
                "auth.html",
                {
                    "error_message": (
                        "You must check the consent box to create an account."
                    ),
                    'active_tab': 'signup',
                },
            )

        # 6. Database checks for duplicates (username, email, or whatsapp number)
        if User.objects.filter(username=username).exists():
            return render(
                request,
                "auth.html",
                {"error_message": "Username is already used.", 'active_tab': 'signup'},
            )

        if User.objects.filter(email=email).exists():
            return render(
                request,
                "auth.html",
                {"error_message": "Email is already used.", 'active_tab': 'signup'},
            )

        if UserProfile.objects.filter(whatsapp_number=whatsapp).exists():
            return render(
                request,
                "auth.html",
                {"error_message": "Number is already used.", 'active_tab': 'signup'},
            )

        # 7. Create user and profile
        user = User.objects.create_user(
            username=username, email=email, password=password
        )
        UserProfile.objects.create(user=user, whatsapp_number=whatsapp, phone_number=whatsapp)

        # Every signup is a customer by default
        customer_group, _ = Group.objects.get_or_create(name="customer")
        user.groups.add(customer_group)

        # Authenticate and login the new user
        login(request, user)
        return redirect("marketplace")

    return render(request, "auth.html")

def logout_view(request):
  logout(request)
  return redirect("signin")  



@login_required
@require_POST
def resolve_dispute_ajax(request, pk):
    dispute = get_object_or_404(Dispute, pk=pk)
    
    # Ensure only the person who opened it, the host, or an authorized user can resolve it
    if dispute.opened_by != request.user and dispute.pool.host != request.user and not request.user.is_staff:
        return JsonResponse({"success": False, "error": "Unauthorized action."}, status=403)
        
    if dispute.status == Dispute.Status.RESOLVED:
        return JsonResponse({"success": False, "error": "Dispute is already resolved."}, status=400)

    # Parse JSON body sent from fetch, with fallback to request.POST just in case
    resolution_text = "Marked as resolved by participant."
    try:
        if request.body:
            data = json.loads(request.body)
            resolution_text = data.get("resolution", resolution_text)
    except json.JSONDecodeError:
        pass
    
    # Fallback check if it came through form data instead
    if resolution_text == "Marked as resolved by participant.":
        resolution_text = request.POST.get("resolution", resolution_text)

    now = timezone.now()
    dispute.status = Dispute.Status.RESOLVED
    dispute.resolution = resolution_text
    dispute.resolved_by = request.user
    dispute.resolved_at = now
    dispute.save()

    # Find the target PoolMember involved in this dispute via the period mapping
   
    # Optionally update pool status back to active if it was disputed
    if hasattr(Pool.Status, 'DISPUTED') and dispute.pool.status == Pool.Status.DISPUTED:
        dispute.pool.status = Pool.Status.OPEN # or active status depending on your choice
        dispute.pool.save()
        
    Notification.objects.create(
        user=dispute.against_user,
        pool=dispute.pool,
        type=Notification.Type.DISPUTE,
        title="Dispute Resolved",
        description=(
            f"The dispute {dispute.name} regarding your "
            f"{dispute.pool.service.name} Shared Pool "
            f"has been resolved."
        ),
        url=f"/dispute/{dispute.pk}/",
    )

    return JsonResponse({"success": True, "message": "Dispute successfully marked as resolved."})


@login_required
@require_GET
def get_banks(key=None):
    """Fetches list of Nigerian banks from Paystack"""
    try:
        response = requests.get(
            "https://api.paystack.co/bank",
            headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"},
            params={"country": "nigeria"}
        )
        
        data = response.json()
        
        return JsonResponse(data, safe=False)
    except Exception as e:
        return JsonResponse({"status": False, "message": str(e)}, status=500)
    

@login_required
@require_POST
def resolve_bank(request):
    try:
        body = json.loads(request.body)
        account_number = body.get("account_number")
        bank_code = body.get("bank_code")
        bank_name = body.get("bank_name")
        account_name = body.get("account_name")

        # Debugging prints to check terminal
        print(f"--- DEBUGGING RESOLVE ---")
        print(f"Account Number: '{account_number}' (Length: {len(str(account_number))})")
        print(f"Bank Code Sent: '{bank_code}'")
        print(f"Bank Name Sent: '{bank_name}'")

        if not account_number or len(account_number) != 10:
            return JsonResponse({"status": False, "message": "A valid 10-digit account number is required."}, status=400)

        if not bank_code:
            return JsonResponse({"status": False, "message": "Please select a valid bank from the list."}, status=400)

        # If account_name wasn't passed, query Paystack to verify it
        if not account_name:
            response = requests.get(
                "https://api.paystack.co/bank/resolve",
                headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}"},
                params={"account_number": account_number, "bank_code": bank_code}
            )
            data = response.json()

            if not data.get("status"):
                return JsonResponse({"status": False, "message": data.get("message", "Account does not exist or invalid details.")}, status=400)
            
            account_name = data["data"]["account_name"]

        # If bank_name and bank_code are provided, save the account details
        if bank_name and bank_code:
            profile, created = UserProfile.objects.get_or_create(user=request.user)
            
            profile.bank = bank_name              # Save Bank Name here
            profile.bank_code = bank_code         # Save Bank Code here
            profile.account_number = encrypt_account_number(account_number)
            profile.account_name = account_name 
            profile.bank_verified = True
            profile.save()

            return JsonResponse({
                "status": True,
                "message": "Account linked successfully!"
            })

        # Otherwise, just return the resolved account name for frontend preview
        return JsonResponse({
            "status": True,
            "account_name": account_name
        })

    except Exception as e:
        print("EXCEPTION IN RESOLVE_BANK:", str(e))
        return JsonResponse({"status": False, "message": str(e)}, status=500)
    
@login_required
def pool(request):
    standard_services = Service.objects.filter(type=Service.Type.STANDARD)
    categories = Service.Category.choices

    # Prefill form context from session if returning back from vault
    pending_pool = request.session.get("pending_pool_data", {})

    if request.method == "POST":
        service_option = request.POST.get("service")
        total_seats_raw = request.POST.get("total_seats")
        price_per_seat_raw = request.POST.get("price_per_seat")
        duration_raw = request.POST.get("duration_days")
        is_public = request.POST.get("is_public") == "on"

        # --- Server-Side Validations ---
        try:
            total_seats = int(total_seats_raw)
            price_per_seat = Decimal(price_per_seat_raw)
            duration_days = int(duration_raw)
        except (TypeError, ValueError):
            messages.error(request, "Please provide valid numeric inputs.")
            return redirect(request.path)

        # Duration must be a proper month: 30 or 31 days only
        if duration_days not in (30, 31):
            messages.error(
                request, "Duration must be a full month: 30 or 31 days."
            )
            return redirect(request.path)

        # Minimum price per seat is 1000 naira
        MIN_PRICE_PER_SEAT = Decimal("1000")
        if price_per_seat < MIN_PRICE_PER_SEAT:
            messages.error(
                request, "Price per seat cannot be less than ₦1,000."
            )
            return redirect(request.path)

        target_service = None
        new_service_name = ""
        new_service_category = ""

        if service_option == "other":
            new_service_name = request.POST.get("new_service_name", "").strip()
            new_service_category = request.POST.get("new_service_category")

            if not new_service_name:
                messages.error(
                    request, "Please enter a name for the new service."
                )
                return redirect(request.path)

            max_allowed_seats = 10
            if total_seats > max_allowed_seats or total_seats < 1:
                messages.error(
                    request,
                    f"Total seats for custom services cannot exceed {max_allowed_seats}.",
                )
                return redirect(request.path)
        else:
            try:
                target_service = Service.objects.get(
                    pk=service_option, type=Service.Type.STANDARD
                )
            except (Service.DoesNotExist, ValueError):
                messages.error(request, "Invalid service selection.")
                return redirect(request.path)

            if total_seats > target_service.max_people or total_seats < 1:
                messages.error(
                    request,
                    f"Total seats cannot exceed {target_service.max_people} for this service.",
                )
                return redirect(request.path)

        # --- Calculate final price (Base price * seats, without platform fee) ---
        total_pool_price = price_per_seat * total_seats

        # Store pool data temporarily in session before saving to database via Vault
        request.session["pending_pool_data"] = {
            "service_option": service_option,
            "target_service_id": (
                target_service.id if target_service else None
            ),
            "new_service_name": new_service_name,
            "new_service_category": new_service_category,
            "total_seats": total_seats,
            "price": str(total_pool_price),
            "duration_days": duration_days,
            "is_public": is_public,
        }

        return redirect("vault")

    context = {
        "standard_services": standard_services,
        "categories": categories,
        "pending_pool": pending_pool,
    }
    return render(request, "pool.html", context)

@login_required
def vault(request):
    pending_pool = request.session.get("pending_pool_data")
    if not pending_pool:
        messages.warning(
            request, "Please set up your pool details before configuring the vault."
        )
        return redirect("pool")

    # Resolve display service name for the vault context header
    service_name = "Custom Service"
    if pending_pool.get("target_service_id"):
        try:
            srv = Service.objects.get(pk=pending_pool["target_service_id"])
            service_name = srv.name
        except Service.DoesNotExist:
            pass
    elif pending_pool.get("new_service_name"):
        service_name = pending_pool["new_service_name"]

    # Fetch user profile data to help populate smart placeholders or defaults
    user_profile, _ = UserProfile.objects.get_or_create(user=request.user)

    if request.method == "POST":
        login_method = request.POST.get("login_method")
        instructions = request.POST.get("instructions", "").strip()

        # Helper function to encrypt values or lists of values safely
        def safe_encrypt(val):
            if not val:
                return ""
            if isinstance(val, list):
                return [encrypt_account_number(str(v)) if v else "" for v in val]
            return encrypt_account_number(str(val))

        # Collect credentials dynamically based on inputs sent from form
        raw_credentials = {}
        if login_method == "email_pass":
            raw_credentials["email"] = request.POST.get("cred_email")
            raw_credentials["password"] = request.POST.get("cred_password")
        elif login_method == "phone_pass":
            raw_credentials["phone"] = request.POST.get("cred_phone")
            raw_credentials["password"] = request.POST.get("cred_password")
        elif login_method == "username_pass":
            raw_credentials["username"] = request.POST.get("cred_username")
            raw_credentials["password"] = request.POST.get("cred_password")
        elif login_method == "email_phone_pass":
            raw_credentials["identifier"] = request.POST.get("cred_identifier")
            raw_credentials["password"] = request.POST.get("cred_password")
        elif login_method == "email_phone_code":
            raw_credentials["identifier"] = request.POST.get("cred_identifier")
        elif login_method == "custom":
            custom_method_name = request.POST.get(
                "custom_method_name", "Custom"
            )
            login_method = f"custom: {custom_method_name}"

            # Grab dynamic key-value pairs and handle duplicate keys safely
            keys = request.POST.getlist("custom_key[]")
            values = request.POST.getlist("custom_value[]")
            
            for k, v in zip(keys, values):
                if k and k.strip():
                    clean_key = k.strip()
                    clean_val = v.strip() if v else ""
                    
                    # If key already exists, turn it into a list or append unique suffix to save all entries
                    if clean_key in raw_credentials:
                        if isinstance(raw_credentials[clean_key], list):
                            raw_credentials[clean_key].append(clean_val)
                        else:
                            raw_credentials[clean_key] = [raw_credentials[clean_key], clean_val]
                    else:
                        raw_credentials[clean_key] = clean_val

        # Encrypt the credential values while keeping the keys plain text
        credentials = {k: safe_encrypt(v) for k, v in raw_credentials.items()}

        # --- Final Commit: Create Service (if custom), Pool, and Vault ---
        target_service = None
        if pending_pool.get("target_service_id"):
            target_service = Service.objects.get(
                pk=pending_pool["target_service_id"]
            )
        else:
            target_service, _ = Service.objects.get_or_create(
                name__iexact=pending_pool["new_service_name"],
                defaults={
                    "name": pending_pool["new_service_name"],
                    "category": pending_pool.get(
                        "new_service_category", Service.Category.OTHER
                    ),
                    "type": Service.Type.MANMADE,
                    "standard_price": Decimal(pending_pool["price"]),
                    "max_people": 10,
                },
            )

        # Create Pool
        new_pool = Pool.objects.create(
            host=request.user,
            service=target_service,
            price=Decimal(pending_pool["price"]),
            number_of_users=pending_pool["total_seats"],
            duration_days=pending_pool["duration_days"],
            is_public=pending_pool["is_public"],
            status=Pool.Status.OPEN,
        )

        # Create Vault
        PoolVault.objects.create(
            pool=new_pool,
            login_method=login_method,
            credentials=credentials,
            instructions=instructions,
        )

        # Clear session storage
        if "pending_pool_data" in request.session:
            del request.session["pending_pool_data"]

        messages.success(
            request, "Your shared pool and vault have been successfully saved!"
        )
        return redirect("marketplace")

    context = {
        "service_name": service_name,
        "pending_pool": pending_pool,
        "user_profile": user_profile,
    }
    return render(request, "vault.html", context)

@login_required
def my_subscriptions_view(request):
    """Main subscriptions dashboard showing limited items per section and all user disputes."""
    user = request.user
    
    # 1. Hosted Pools (Open or Close)
    hosted_pools = Pool.objects.filter(
        host=user, 
        status__in=[Pool.Status.OPEN, Pool.Status.CLOSE]
    ).order_by('-created_at')[:8]
    
    # 2. Joined Pools (Where user is an active pool member)
    joined_pools = Pool.objects.filter(
        members__user=user, 
        members__active=True
    ).exclude(host=user).order_by('-created_at')[:8]
    
    # 3. Successful Pools (Completed status)
    successful_pools = Pool.objects.filter(
        Q(host=user) | Q(members__user=user, members__active=True),
        status=Pool.Status.COMPLETED
    ).distinct().order_by('-created_at')[:8]

    # 4. Cancelled Pools (Cancelled status)
    cancelled_pools = Pool.objects.filter(
        Q(host=user) | Q(members__user=user, members__active=True),
        status=Pool.Status.CANCELLED
    ).distinct().order_by('-created_at')[:8]

    # 5. All Disputes involving user as opener or against_user
    disputes = Dispute.objects.filter(
        Q(opened_by=user) | Q(against_user=user)
    ).distinct().order_by('-created_at')

    context = {
        'hosted_pools': hosted_pools,
        'joined_pools': joined_pools,
        'successful_pools': successful_pools,
        'cancelled_pools': cancelled_pools,
        'disputes': disputes,
    }
    return render(request, 'subscription.html', context)


@login_required
def subscription_list_view(request, filter_type):
    """View to 'See All' pools filtered by parameter: host, is_in, successful, cancelled."""
    user = request.user
    pools = Pool.objects.none()
    title = ""

    if filter_type == 'host':
        pools = Pool.objects.filter(
            host=user, 
            status__in=[Pool.Status.OPEN, Pool.Status.CLOSE]
        )
        title = "All Hosted Pools (Open & Close)"
        
    elif filter_type == 'is_in':
        pools = Pool.objects.filter(
            members__user=user, 
            members__active=True
        )
        title = "All Joined Pools (Active Memberships)"
        
    elif filter_type == 'successful':
        pools = Pool.objects.filter(
            Q(host=user) | Q(members__user=user, members__active=True),
            status=Pool.Status.COMPLETED
        ).distinct()
        title = "All Successful Pools"
        
    elif filter_type == 'cancelled':
        pools = Pool.objects.filter(
            Q(host=user) | Q(members__user=user, members__active=True),
            status=Pool.Status.CANCELLED
        ).distinct()
        title = "All Cancelled Pools"
        
    pools = pools.order_by('-created_at')

    context = {
        'pools': pools,
        'filter_type': filter_type,
        'title': title,
    }
    return render(request, 'subscription_all.html', context)


# Decryption utility as provided
def decrypt_account_number(encrypted_account):
    try:
        key = base64.urlsafe_b64decode(settings.ACCOUNT_ENCRYPTION_KEY)
        data = base64.urlsafe_b64decode(encrypted_account)
        nonce = data[:12]
        encrypted = data[12:]
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, encrypted, None).decode()
    except Exception:
        return "[Decryption Failed]"

@login_required(login_url="signin")
def get_pool_vault_credentials(request, pk):
    if request.method != "POST":
        return JsonResponse({"error": "Invalid method"}, status=405)
        
    pool = get_object_or_404(Pool, pk=pk)
    is_host = pool.host == request.user
    
    # Check access validation
    is_paid_member = False
    if not is_host:
        membership = PoolMember.objects.filter(
            pool=pool, 
            user=request.user, 
            payment_status=PoolMember.PaymentStatus.PAID,
            active=True
        ).first()
        is_paid_member = membership is not None

    if not is_host and not is_paid_member:
        return JsonResponse({"error": "Unauthorized access to vault"}, status=403)

    # Confirm user password for security modal
    password = request.POST.get("password")
    if not password or not check_password(password, request.user.password):
        return JsonResponse({"error": "Incorrect password confirmation"}, status=400)

    try:
        vault = pool.vault
    except PoolVault.DoesNotExist:
        return JsonResponse({"error": "Vault not configured for this pool"}, status=404)

    # Decrypt JSON credentials dictionary
    decrypted_credentials = {}
    if vault.credentials and isinstance(vault.credentials, dict):
        for field_key, enc_val in vault.credentials.items():
            decrypted_credentials[field_key] = decrypt_account_number(enc_val)

    return JsonResponse({
        "login_method": vault.login_method,
        "credentials": decrypted_credentials,
        "instructions": vault.instructions,
        "updated_at": vault.updated_at.strftime("%b. %d, %Y, %I:%M %p")
    })




@login_required(login_url="signin")
def market_detail(request, pk):
    pool = get_object_or_404(Pool, pk=pk)
    
    auto_confirm_access(pool)

    # Check if current user is the host
    is_host = pool.host == request.user or request.user.groups.filter(name="admin").exists()
    def_host = pool.host == request.user
    an_admin = request.user.groups.filter(name="admin").exists() 

    # Check membership details for contributors
    membership = None
    is_member = False
    is_paid = False
    confirmed_access = False
    current_step = 1

    if not is_host:
        membership = PoolMember.objects.filter(
            pool=pool,
            user=request.user,
            active=True
        ).first()

        is_member = membership is not None
        is_paid = membership and membership.payment_status == PoolMember.PaymentStatus.PAID
        confirmed_access = membership and membership.confirmed_access

        # Determine active progress step
        if not is_member:
            current_step = 1
        elif not is_paid:
            current_step = 2
        elif not confirmed_access:
            current_step = 3
        else:
            current_step = 4

    # Pool calculations
    start_date = pool.created_at
    end_date = start_date + timedelta(days=pool.duration_days)

    now = timezone.now()
    elapsed_days = (now - start_date).days
    remaining_days = max(0, pool.duration_days - elapsed_days)

    # Only active members count toward pool capacity
    current_users_count = pool.members.filter(active=True).count()
    available_slots = max(0, pool.number_of_users - current_users_count)

    host_reputation = calculate_reputation(pool.host)

    # Vault access validation
    can_access_vault = is_host or (
        membership and is_paid
    )

    # --- REPLACED SINGLE DISPUTE LOGIC WITH MULTIPLE DISPUTES QUERYSET ---
    active_disputes = Dispute.objects.filter(pool=pool).exclude(status=Dispute.Status.RESOLVED)
    
    if not is_host:
        # Contributors only see disputes they opened or that are filed against them
        active_disputes = active_disputes.filter(
            Q(opened_by=request.user) | Q(against_user=request.user)
        )
    # ---------------------------------------------------------------------

    is_paid_member = (
        not is_host
        and membership is not None
        and membership.payment_status == PoolMember.PaymentStatus.PAID
    )
    # --- RATING VISIBILITY & STATE LOGIC ---
    # Check if the user is an active pool member (ignoring hosts, as hosts shouldn't rate their own pool)
    active_membership = pool.members.filter(user=request.user, active=True).first()
    is_active_pool_member = active_membership is not None

    # Condition: Pool must be completed AND user must be an active member
    show_rating_compartment = (
        pool.status == Pool.Status.COMPLETED and is_active_pool_member
    )
    decrypted_account = ""
    if hasattr(request.user, 'profile') and request.user.profile.account_number:
        decrypted_account = decrypt_account_number(request.user.profile.account_number)
    # Check if the user has already rated this pool
    user_rating = None
    if show_rating_compartment:
        user_rating = Rating.objects.filter(pool=pool, reviewer=request.user).first()
    # ---------------------------------------
    is_active_member = membership is not None and membership.active
    show_earnings_report = not pool.active and (is_host or is_active_member)
    site_info = SiteInfo.objects.first() # or however you fetch your site settings
    site_percentage = site_info.site_percentage if site_info and site_info.site_percentage else Decimal("0.00")
    flat_price = site_info.price if site_info and site_info.price else Decimal("0.00")

    # calculation: original split + percentage part + flat price
    amount_split = pool.amount_per_person
    percentage_amount = amount_split * (site_percentage / Decimal("100.00"))
    calculated_copay_total = amount_split + percentage_amount + flat_price
    user_payment = None
    if membership:
        user_payment = membership.payments.filter(status=Payment.Status.PAID).first()
    # Only active members count toward pool capacity
    current_users_count = pool.members.filter(active=True).count()
    available_slots = max(0, pool.number_of_users - current_users_count)
    
    # Check if there are no active pool members
    has_no_active_members = current_users_count == 0
    context = {
        "has_no_active_members": has_no_active_members,
        "current_users_count": current_users_count,
        "user_payment": user_payment,
        "calculated_copay_total": calculated_copay_total,
        "show_earnings_report": show_earnings_report,
        "show_rating_compartment": show_rating_compartment,
        "user_rating": user_rating, # Passes the rating object if it exists (e.g., has .rating)
        "is_paid_member": is_paid_member,
        "active_disputes": active_disputes,  # Passed as a queryset for looping
        "pool_contributors": pool.members.filter(
            payment_status=PoolMember.PaymentStatus.PAID,
            active=True
        ).select_related("user"),
        "pool": pool,
        "is_host": is_host,
        "def_host": def_host,
        "an_admin": an_admin,
        "membership": membership,
        "is_member": is_member,
        "decrypted_account": decrypted_account,
        "can_access_vault": can_access_vault,
        "host_reputation": host_reputation,
        "is_paid": is_paid,
        "confirmed_access": confirmed_access,
        "current_step": current_step,
        "start_date": start_date,
        "end_date": end_date,
        "remaining_days": remaining_days,
        "current_users_count": current_users_count,
        "available_slots": available_slots,
    }

    return render(request, "market.html", context)


@login_required
@require_POST
def submit_pool_rating(request, pk):
    pool = get_object_or_404(Pool, pk=pk)

    # Validate rules server-side
    if pool.status != Pool.Status.COMPLETED:
        return JsonResponse(
            {"error": "This pool is not completed yet."},
            status=400
        )

    is_active_member = pool.members.filter(
        user=request.user,
        active=True
    ).exists()

    if not is_active_member:
        return JsonResponse(
            {"error": "Only active pool members can rate this pool."},
            status=403
        )

    # Check if already rated
    if Rating.objects.filter(
        pool=pool,
        reviewer=request.user
    ).exists():
        return JsonResponse(
            {"error": "You have already rated this pool."},
            status=400
        )

    try:
        data = json.loads(request.body)
        rating_value = int(data.get("rating", 0))

        if not (1 <= rating_value <= 5):
            return JsonResponse(
                {"error": "Rating must be between 1 and 5."},
                status=400
            )

        # Create rating
        Rating.objects.create(
            pool=pool,
            reviewer=request.user,
            host=pool.host,
            rating=rating_value
        )

        # Notify the host
        Notification.objects.create(
            user=pool.host,
            pool=pool,
            type=Notification.Type.RATING,
            title="You Received a New Rating",
            description=(
                f"@{request.user.username} rated your "
                f"{pool.service.name} Shared Pool "
                f"{rating_value}/5."
            ),
            url=f"/market/{pool.pk}/",
        )

        return JsonResponse(
            {
                "success": True,
                "rating": rating_value
            },
            status=201
        )

    except Exception as e:
        return JsonResponse(
            {"error": str(e)},
            status=400
        )

@login_required(login_url="signin")
@require_POST
def handle_access_confirmation(request, pk):
    pool = get_object_or_404(Pool, pk=pk)

    membership = get_object_or_404(
        PoolMember,
        pool=pool,
        user=request.user,
        payment_status=PoolMember.PaymentStatus.PAID,
        active=True
    )

    action = request.POST.get("action")

    if action == "confirm":
        now = timezone.now()

        membership.confirmed_access = True
        membership.confirmed_at = now
        membership.joined_at = now

        membership.save(
            update_fields=[
                "confirmed_access",
                "confirmed_at",
                "joined_at",
            ]
        )

        # Count officially joined contributors
        current_users = pool.members.filter(
            active=True,
            confirmed_access=True,
        ).count()

        # Notify the host
        Notification.objects.create(
            user=pool.host,
            pool=pool,
            type=Notification.Type.POOL,
            title="New Contributor Joined",
            description=(
                f"@{request.user.username} has confirmed access and "
                f"officially joined your {pool.service.name} Shared Pool. "
                f"Slot capacity is now "
                f"{current_users}/{pool.number_of_users} filled."
            ),
            url=f"/market/{pool.pk}/",
        )

        return JsonResponse({
            "status": "success",
            "state": "confirmed",
        })

    return JsonResponse(
        {"error": "Invalid action"},
        status=400
    )

@login_required(login_url="signin")
@require_POST
def open_general_dispute(request, pk):
    pool = get_object_or_404(Pool, pk=pk)
    is_host = pool.host == request.user

    if not is_host:
        membership = PoolMember.objects.filter(
            pool=pool, user=request.user, active=True,
            payment_status=PoolMember.PaymentStatus.PAID,
        ).first()
        if not membership:
            return JsonResponse({'error': 'Unauthorized participant.'}, status=403)
        against_user = pool.host
        target_member = membership
    else:
        against_user_id = request.POST.get('against_user_id')
        against_user = get_object_or_404(User, pk=against_user_id)
        if against_user == request.user:
            return JsonResponse({'error': 'You cannot dispute yourself.'}, status=400)
        target_member = get_object_or_404(PoolMember, pool=pool, user=against_user, active=True)

    reason = request.POST.get('reason')
    if not reason:
        return JsonResponse({'error': 'Reason is required.'}, status=400)

    debug_print("========== OPEN DISPUTE ==========")
    debug_print("opened_by:", request.user.id, "against_user:", against_user.id, "is_host_opening:", is_host)

    while True:
        dispute_name = f"#DSP-{random.randint(1000, 9999)}"
        if not Dispute.objects.filter(name=dispute_name).exists():
            break

    now = timezone.now()

    dispute = Dispute.objects.create(
        pool=pool, name=dispute_name, opened_by=request.user,
        against_user=against_user, reason=reason, status=Dispute.Status.OPEN,
    )
    debug_print("[DISPUTE] created:", dispute.name)

    target_member.dispute = True
    target_member.save(update_fields=["dispute"])
  

    Notification.objects.create(
        user=against_user, pool=pool, type=Notification.Type.DISPUTE,
        title="New Dispute Opened",
        description=(
            f"@{request.user.username} has opened a dispute against you regarding your "
            f"{pool.service.name} Shared Pool. Dispute {dispute.name}."
        ),
        url=f"/dispute/{dispute.pk}/",
    )

    return JsonResponse({'status': 'success', 'dispute_id': dispute.id})   




@login_required
@require_POST
def leave_pool_ajax(request, pk):
    pool = get_object_or_404(Pool, pk=pk)

    membership = PoolMember.objects.filter(
        pool=pool,
        user=request.user,
        active=True
    ).first()

    debug_print("========== LEAVE POOL ==========")
    debug_print("user:", request.user.id, "pool:", pool.id, "membership found:", bool(membership))

    if not membership:
        return JsonResponse(
            {"success": False, "error": "Active membership not found."},
            status=400,  # was status=00, which is not a valid HTTP status
        )

    now = timezone.now()

    # 1. Freeze the stint: mark exactly when they left, deactivate.
   
    #    still needs it below to compute this stint's final elapsed days.
    membership.left_at = now
    membership.active = False
    membership.confirmed_access = False
    membership.confirmed_at = None
    membership.save(update_fields=[
        "left_at",
        "active",
        "confirmed_access",
        "confirmed_at",
    ])
    debug_print("[LEAVE] membership deactivated, left_at set to", now)

    # 2. Freeze this stint's final earned amount. Because active=False and
 
    #    and writes the final number to THIS stint's PoolEarning row
    #    (keyed by stint_started_at=joined_at, so it won't touch a future stint).
    
    debug_print("[LEAVE] earnings frozen for this stint")

    current_users = pool.members.filter(active=True).count()

    if pool.status == Pool.Status.CLOSE and current_users < pool.number_of_users:
        pool.status = Pool.Status.OPEN
        pool.save(update_fields=["status"])
        debug_print("[LEAVE] pool reopened, slots available")

    Notification.objects.create(
        user=pool.host,
        pool=pool,
        type=Notification.Type.POOL,
        title="Contributor Left",
        description=(
            f"@{request.user.username} has left your "
            f"{pool.service.name} Shared Pool. "
            f"Slot capacity is now {current_users}/{pool.number_of_users} filled."
        ),
        url=f"/market/{pool.pk}/",
    )

    return JsonResponse({"success": True})
    



@login_required(login_url="signin")
@require_POST
def cancel_pool_ajax(request, pk):
    pool = get_object_or_404(Pool, pk=pk)

    # Only the host can cancel
    if pool.host != request.user:
        return JsonResponse(
            {
                "success": False,
                "error": "Unauthorized action.",
            },
            status=403,
        )

    if pool.status == Pool.Status.CANCELLED:
        return JsonResponse(
            {
                "success": False,
                "error": "Pool is already cancelled.",
            },
            status=400,
        )

    now = timezone.now()

    # 1. Mark all currently active members as having left
    active_members = PoolMember.objects.filter(
        pool=pool,
        active=True,
    )

    for member in active_members:
        # Notify the member before deactivating them
        Notification.objects.create(
            user=member.user,
            pool=pool,
            type=Notification.Type.POOL,
            title="Pool Cancelled",
            description=(
                f"The {pool.service.name} Shared Pool hosted by "
                f"@{pool.host.username} has been cancelled."
            ),
            url=f"/market/{pool.pk}/",
        )
        member.left_at = now
        member.active = False
        member.confirmed_access = False
        member.confirmed_at = None

        member.save(
            update_fields=[
                "left_at",
                "active",
                "confirmed_access",
                "confirmed_at",
            ]
        )

    # 2. Mark pool as cancelled
    # Keep active=True for now because the earnings utility
    # only calculates earnings for active pools.
    pool.status = Pool.Status.CANCELLED
    pool.end_date = now
    pool.save(update_fields=["status", "end_date"])

    # 3. Calculate final earnings for the pool
  



    # 5. Only deactivate the pool AFTER settlement
    pool.active = False
    pool.save(update_fields=["active"])

    return JsonResponse(
        {
            "success": True,
            "message": "Pool successfully cancelled and settlement completed.",
        }
    )
    


@login_required
def edit_vault(request, pk):
    vault = get_object_or_404(PoolVault, pk=pk, pool__host=request.user)
    pool = vault.pool
    service_name = pool.service.name if pool.service else "Custom Service"

    user_profile, _ = UserProfile.objects.get_or_create(user=request.user)

    # Decrypt stored credentials for form pre-population
    decrypted_credentials = {}

    if vault.credentials:
        for k, v in vault.credentials.items():
            if isinstance(v, list):
                decrypted_credentials[k] = [
                    decrypt_account_number(item) if item else ""
                    for item in v
                ]
            elif v:
                try:
                    decrypted_credentials[k] = decrypt_account_number(v)
                except Exception:
                    decrypted_credentials[k] = v
            else:
                decrypted_credentials[k] = ""

    # Parse login method
    active_login_method = vault.login_method
    custom_method_name = ""

    if vault.login_method and vault.login_method.startswith("custom:"):
        active_login_method = "custom"
        custom_method_name = vault.login_method.replace("custom:", "").strip()

    if request.method == "POST":

        login_method = request.POST.get("login_method")
        instructions = request.POST.get("instructions", "").strip()

        def safe_encrypt(val):
            if not val:
                return ""

            if isinstance(val, list):
                return [
                    encrypt_account_number(str(v)) if v else ""
                    for v in val
                ]

            return encrypt_account_number(str(val))

        raw_credentials = {}

        if login_method == "email_pass":
            raw_credentials["email"] = request.POST.get("cred_email")
            raw_credentials["password"] = request.POST.get("cred_password")

        elif login_method == "phone_pass":
            raw_credentials["phone"] = request.POST.get("cred_phone")
            raw_credentials["password"] = request.POST.get("cred_password")

        elif login_method == "username_pass":
            raw_credentials["username"] = request.POST.get("cred_username")
            raw_credentials["password"] = request.POST.get("cred_password")

        elif login_method == "email_phone_pass":
            raw_credentials["identifier"] = request.POST.get("cred_identifier")
            raw_credentials["password"] = request.POST.get("cred_password")

        elif login_method == "email_phone_code":
            raw_credentials["identifier"] = request.POST.get("cred_identifier")

        elif login_method == "custom":
            custom_name = request.POST.get("custom_method_name", "Custom")
            login_method = f"custom: {custom_name}"

            keys = request.POST.getlist("custom_key[]")
            values = request.POST.getlist("custom_value[]")

            for k, v in zip(keys, values):
                if k and k.strip():
                    clean_key = k.strip()
                    clean_val = v.strip() if v else ""

                    if clean_key in raw_credentials:
                        if isinstance(raw_credentials[clean_key], list):
                            raw_credentials[clean_key].append(clean_val)
                        else:
                            raw_credentials[clean_key] = [
                                raw_credentials[clean_key],
                                clean_val,
                            ]
                    else:
                        raw_credentials[clean_key] = clean_val

        credentials = {
            k: safe_encrypt(v)
            for k, v in raw_credentials.items()
        }

        with transaction.atomic():

            # Save updated vault
            vault.login_method = login_method
            vault.credentials = credentials
            vault.instructions = instructions
            vault.save()

            # Get active, paid contributors
            members = pool.members.filter(
                active=True,
                payment_status=PoolMember.PaymentStatus.PAID,
            ).exclude(
                user=request.user
            )

            # Notify every eligible contributor
            notifications = [
                Notification(
                    user=member.user,
                    pool=pool,
                    type=Notification.Type.SECURITY,
                    title="Pool Vault Updated",
                    description=(
                        f"The host has updated the vault details for "
                        f"{service_name}. Please visit the pool to review "
                        f"the latest access information."
                    ),
                    url=reverse("market_detail", kwargs={"pk": pool.pk}),
                )
                for member in members
            ]

            if notifications:
                Notification.objects.bulk_create(notifications)

        messages.success(
            request,
            "Vault details updated successfully. Active members have been notified!"
        )

        return redirect("market_detail", pk=pool.pk)

    context = {
        "vault": vault,
        "pool": pool,
        "service_name": service_name,
        "user_profile": user_profile,
        "decrypted_credentials": decrypted_credentials,
        "active_login_method": active_login_method,
        "custom_method_name": custom_method_name,
    }

    return render(request, "edit_vault.html", context)







@login_required
def update_profile_ajax(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            email = data.get('email', '').strip()
            username = data.get('username', '').strip()
            phone_number = data.get('phone_number', '').strip()
            whatsapp_number = data.get('whatsapp_number', '').strip()

            # 1. Validation for Phone Format (234 + 10 digits)
            phone_regex = re.compile(r'^234\d{10}$')
            if not phone_regex.match(phone_number):
                return JsonResponse({'error': 'Phone number must start with 234 and contain 10 digits after.'}, status=400)
            
            if whatsapp_number and not phone_regex.match(whatsapp_number):
                return JsonResponse({'error': 'WhatsApp number must start with 234 and contain 10 digits after.'}, status=400)

            # 2. Check if Email is already used by another user
            if User.objects.filter(email=email).exclude(pk=request.user.pk).exists():
                return JsonResponse({'error': 'This email address is already registered by another account.'}, status=400)

            # 3. Check if Username is already used by another user
            if User.objects.filter(username=username).exclude(pk=request.user.pk).exists():
                return JsonResponse({'error': 'This username is already taken.'}, status=400)

            # 4. Update core User details
            user = request.user
            user.email = email
            user.username = username
            user.save()

            # 5. Update Profile details
            profile = user.profile
            profile.phone_number = phone_number
            profile.whatsapp_number = whatsapp_number
            profile.save()

            return JsonResponse({
                'message': 'Profile updated successfully!',
                'email': user.email,
                'username': user.username,
                'phone_number': profile.phone_number,
                'whatsapp_number': profile.whatsapp_number
            }, status=200)

        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON data payload.'}, status=400)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)

    return JsonResponse({'error': 'Invalid request method.'}, status=405)



@login_required
@require_POST
def verify_bank_password(request):
    """Ajax endpoint to check password before revealing encrypted bank details"""
    try:
        body = json.loads(request.body)
        password = body.get("password")

        if not request.user.check_password(password):
            return JsonResponse({"status": False, "message": "Incorrect password provided."}, status=400)

        profile = getattr(request.user, 'profile', None)
        decrypted_acc = ""
        if profile and profile.account_number:
            try:
                decrypted_acc = decrypt_account_number(profile.account_number)
            except Exception:
                decrypted_acc = ""

        return JsonResponse({
            "status": True,
            "decrypted_account": decrypted_acc
        })
    except Exception as e:
        return JsonResponse({"status": False, "message": str(e)}, status=500)



@login_required
@require_POST
def change_password(request):
    try:
        body = json.loads(request.body)
        old_password = body.get("old_password")
        new_password = body.get("new_password")

        if not old_password or not new_password:
            return JsonResponse({"status": False, "message": "Both old and new passwords are required."}, status=400)

        # Check if old password matches
        if not request.user.check_password(old_password):
            return JsonResponse({"status": False, "message": "Incorrect old password provided."}, status=400)

        # Enforce server-side rule: 8 characters long with at least a digit
        has_digit = any(char.isdigit() for char in new_password)
        if len(new_password) < 8 or not has_digit:
            return JsonResponse({
                "status": False, 
                "message": "New password must be at least 8 characters long and contain at least one digit."
            }, status=400)

        # Set and save the new password
        request.user.set_password(new_password)
        request.user.save()

        # Crucial: Prevents user from being logged out automatically after password update
        update_session_auth_hash(request, request.user)

        return JsonResponse({
            "status": True,
            "message": "Password updated successfully!"
        })

    except Exception as e:
        return JsonResponse({"status": False, "message": str(e)}, status=500)
    


@login_required
def history(request):
    # Fetch all payment histories for the logged-in user, ordered by recently created
    payment_list = PaymentHistory.objects.filter(user=request.user).order_by("-created_at")
    
    # Set up pagination (e.g., 10 items per page)
    paginator = Paginator(payment_list, 15)
    page_number = request.GET.get("page")
    recent_payments = paginator.get_page(page_number)

    context = {
        "recent_payments": recent_payments,
    }
    return render(request, "history.html", context)


@login_required
def payment_receipt(request, pk):
    # Fetch the payment history record securely for the logged-in user
    payment = get_object_or_404(PaymentHistory, pk=pk, user=request.user)
    
    # Determine if the current user is the host of the pool or a contributor
    is_host = payment.pool.host == request.user
    user_role = "Host" if is_host else "Contributor"

    context = {
        "payment": payment,
        "user_role": user_role,
    }
    return render(request, "receipt.html", context)



@login_required
def notification(request):
    user_notifications = request.user.notifications.all().order_by("-created_at")
    unread_count = user_notifications.filter(is_read=False).count()

    context = {
        "notifications": user_notifications,
        "unread_count": unread_count,
    }

    return render(request, "notification.html", context)


@login_required
@require_POST
def mark_notification_read(request, pk):
  notification = get_object_or_404(Notification, pk=pk, user=request.user)
  notification.is_read = True
  notification.save()
  return JsonResponse({'status': 'success'})


@login_required
@require_POST
def mark_all_notifications_read(request):
  request.user.notifications.filter(is_read=False).update(is_read=True)
  return JsonResponse({'status': 'success'})



logger = logging.getLogger(__name__)

PAYSCROW_START_URL = "https://api.payscrow.net/api/v3/marketplace/transactions/start"
# The placeholder can be {ref} or {number}; build_status_url() handles both.
PAYSCROW_STATUS_URL = "https://api.payscrow.net/api/v3/marketplace/transactions/{ref}/status"

# Trim these once the prints show you the exact values Payscrow sends for a paid transaction
PAID_STATUSES = {"successful", "success", "paid", "funded", "completed", "fundsecured", "secured", "inprogress"}

TWO_PLACES = Decimal("0.01")


# ---------------------------------------------------------------------------
# 0. HELPERS
# ---------------------------------------------------------------------------
def q2(value):
    """Round a Decimal to 2 places."""
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def debug_print(*args):
    """Prints only when DEBUG=True, so nothing leaks in production."""
    if settings.DEBUG:
        print(*args)


def get_remaining_days(pool, now=None):
    """Calendar-day based. Returns 0 if the pool has expired."""
    now = now or timezone.now()

    if pool.end_date and now >= pool.end_date:
        return 0

    elapsed = (timezone.localtime(now).date() - timezone.localtime(pool.created_at).date()).days
    elapsed = max(0, elapsed)
    return max(0, pool.duration_days - elapsed)


def extract_transaction_guid(data):
    """
    Looks for the transaction GUID in a Payscrow response dict.
    Payscrow's docs call it "transactionId" (the GUID), not transactionNumber.
    """
    if not isinstance(data, dict):
        return None

    for key in ("transactionId", "transactionGuid", "transactionID", "TransactionId", "guid"):
        value = data.get(key)
        if value:
            return str(value).strip() or None

    return None


def build_status_url(reference):
    """Works whether the URL constant uses {ref} or {number}, so no more KeyError."""
    return (
        PAYSCROW_STATUS_URL
        .replace("{ref}", str(reference))
        .replace("{number}", str(reference))
    )


def attach_guid(payment, guid):
    """
    Sets payment.transaction_guid in memory (caller must save).
    Skips if already set, empty, or already used by another payment (field is unique).
    """
    if payment.transaction_guid or not guid:
        return False

    guid = str(guid).strip()
    if not guid:
        return False

    if Payment.objects.filter(transaction_guid=guid).exclude(pk=payment.pk).exists():
        logger.error("GUID %s already belongs to another payment", guid)
        return False

    payment.transaction_guid = guid
    return True


# ---------------------------------------------------------------------------
# 1. INITIALIZE PAYMENT
# ---------------------------------------------------------------------------
@login_required
@require_POST
def initialize_payscrow_payment(request, pool_id):
    pool = get_object_or_404(Pool, id=pool_id)
    site_info = SiteInfo.objects.first()

    # --- Validation checks ---
    if pool.host == request.user:
        return JsonResponse(
            {"success": False, "message": "Hosts cannot join their own pools as contributors."},
            status=400,
        )

    if not pool.active or pool.status != Pool.Status.OPEN:
        return JsonResponse(
            {"success": False, "message": "This pool is no longer active or available to join."},
            status=400,
        )

    if pool.current_users >= pool.number_of_users:
        return JsonResponse(
            {"success": False, "message": "This pool is already full."},
            status=400,
        )

    if PoolMember.objects.filter(pool=pool, user=request.user, active=True).exists():
        return JsonResponse(
            {"success": False, "message": "You are already an active member of this pool."},
            status=400,
        )

    # --- Expiry check (only blocks expired pools, does NOT change the price) ---
    now = timezone.now()
    remaining_days = get_remaining_days(pool, now)

    if remaining_days <= 0:
        return JsonResponse(
            {"success": False, "message": "This pool has expired and can no longer be joined."},
            status=400,
        )

    # --- Pricing: flat share, NOT time based ---
    # e.g. pool price 5000, 2 people -> every member pays 2500
    contribution = q2(pool.price / Decimal(str(max(pool.number_of_users, 1))))

    if contribution <= 0:
        return JsonResponse(
            {"success": False, "message": "No contribution remains for this pool."},
            status=400,
        )

    # --- Optional SiteInfo handling (empty = 0) ---
    site_percentage = (
        site_info.site_percentage if (site_info and site_info.site_percentage is not None)
        else Decimal("0.00")
    )
    flat_price = (
        site_info.price if (site_info and site_info.price is not None)
        else Decimal("0.00")
    )

    copay_percentage_fee = q2(contribution * (site_percentage / Decimal("100")))
    copay_flat_fee = q2(flat_price)
    total_copay_fees = q2(copay_percentage_fee + copay_flat_fee)

    # Host share + CoPay fees = price sent to PayScrow
    total_item_price = q2(contribution + total_copay_fees)

    if total_copay_fees > 0 and (not site_info or not site_info.account_number or not site_info.bank_code):
        return JsonResponse(
            {"success": False, "message": "Site billing settlement accounts are not fully configured."},
            status=400,
        )

    # --- Phone numbers & Profiles ---
    def format_phone(phone_str):
        if not phone_str:
            return None
        cleaned = str(phone_str).strip().replace("+", "").replace(" ", "")
        if cleaned.startswith("0"):
            return "234" + cleaned[1:]
        return cleaned

    customer_profile = get_object_or_404(UserProfile, user=request.user)
    host_profile = get_object_or_404(UserProfile, user=pool.host)

    customer_phone = format_phone(customer_profile.phone_number)
    host_phone = format_phone(host_profile.phone_number)

    if not customer_phone or len(customer_phone) < 10:
        return JsonResponse(
            {"success": False, "message": "Please update your profile with a valid phone number before joining a pool."},
            status=400,
        )

    if not host_phone or len(host_phone) < 10:
        return JsonResponse(
            {"success": False, "message": "The pool host has an invalid or missing phone number."},
            status=400,
        )

    host_account_number = decrypt_account_number(host_profile.account_number)
    if not host_account_number or len(str(host_account_number).strip()) != 10:
        return JsonResponse(
            {"success": False, "message": "The pool host's account number is missing or invalid."},
            status=400,
        )

    # --- Payload ---
    transaction_number = f"COPAY-Pool{pool.id}-{request.user.id}-{uuid.uuid4().hex[:12]}"
    callback_url = request.build_absolute_uri(f"/market/{pool.id}/callback/")

    settlement_accounts = [
        {
            "accountNumber": str(host_account_number).strip(),
            "accountName": host_profile.account_name,
            "bankCode": str(host_profile.bank_code),
            "amount": float(contribution),
        }
    ]

    if total_copay_fees > 0:
        settlement_accounts.append({
            "accountNumber": str(site_info.account_number).strip(),
            "accountName": site_info.account_name,
            "bankCode": str(site_info.bank_code),
            "amount": float(total_copay_fees),
        })

    # Settlements (host + CoPay) must equal the item price exactly
    settlement_total = q2(sum(Decimal(str(a["amount"])) for a in settlement_accounts))
    if settlement_total != total_item_price:
        logger.error(
            "Settlement mismatch for pool %s user %s: settlements=%s item=%s",
            pool.id, request.user.id, settlement_total, total_item_price,
        )
        return JsonResponse(
            {"success": False, "message": "Pricing error. Please try again or contact support."},
            status=500,
        )

    payload = {
        "transactionReference": transaction_number,
        "returnUrl": callback_url,
        "customerName": request.user.get_full_name() or request.user.username,
        "customerEmailAddress": request.user.email,
        "customerPhoneNo": customer_phone,
        "merchantName": pool.host.get_full_name() or pool.host.username,
        "merchantEmailAddress": pool.host.email,
        "merchantPhoneNo": host_phone,
        "currencyCode": "NGN",
        "merchantChargePercentage": 0,
        "items": [
            {
                "name": pool.service.name,
                "description": f"Subscription pool slot for {pool.service.name} hosted by {pool.host.username}",
                "quantity": 1,
                "price": float(total_item_price),
            }
        ],
        "settlementAccounts": settlement_accounts,
    }

    # Webhook only works on a public HTTPS URL (not localhost)
    webhook_url = request.build_absolute_uri("/market/payscrow/webhook/")
    if webhook_url.startswith("https://") and "127.0.0.1" not in webhook_url and "localhost" not in webhook_url:
        payload["webhookNotificationUrl"] = webhook_url
        
    debug_print("[INIT] webhook_url:", webhook_url, "| sent to Payscrow:", "webhookNotificationUrl" in payload)

    headers = {
        "BrokerApiKey": settings.PAYSCROW_API_KEY,
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(PAYSCROW_START_URL, json=payload, headers=headers, timeout=30)
        res_data = response.json()
    except (requests.RequestException, ValueError):
        logger.exception("PayScrow initialization connection/parse error")
        return JsonResponse(
            {"success": False, "message": "Unable to connect to payment provider. Please try again."},
            status=500,
        )

    debug_print("[INIT] PAYSCROW START RESPONSE:", res_data)

    if not (response.status_code == 200 and res_data.get("success")):
        err_detail = res_data.get("message")
        if not err_detail and isinstance(res_data.get("data"), dict):
            errors_list = res_data["data"].get("errors")
            if errors_list:
                err_detail = " | ".join(errors_list) if isinstance(errors_list, list) else str(errors_list)
        err_msg = err_detail or "Failed to initialize escrow transaction with PayScrow."
        logger.warning("PayScrow init rejected: %s | %s", err_msg, res_data)
        return JsonResponse({"success": False, "message": err_msg}, status=400)

    data_content = res_data.get("data") or {}
    payment_link = data_content.get("paymentLink")

    if not payment_link:
        logger.error("PayScrow init succeeded but returned no paymentLink: %s", res_data)
        return JsonResponse(
            {"success": False, "message": "Payment provider did not return a payment link. Please try again."},
            status=502,
        )

    payscrow_number = (
        data_content.get("transactionReference")
        or data_content.get("transactionNumber")
        or transaction_number
    )

    # The /start response usually has NO GUID. It is captured later from the
    # webhook or the verify response. `or None` guarantees we never save "".
    transaction_guid = (
        extract_transaction_guid(data_content) or extract_transaction_guid(res_data) or None
    )

    debug_print("[INIT] keys in Payscrow data:", list(data_content.keys()))
    debug_print("[INIT] transaction_guid:", transaction_guid or "NOT in /start response (will come from webhook/verify)")
    debug_print("[INIT] our reference:", transaction_number)
    debug_print("[INIT] reference being saved:", payscrow_number)
    debug_print("[INIT] item price (host + copay):", total_item_price)
    debug_print("[INIT] PayScrow totalPayable (incl. their fee):", data_content.get("totalPayable"))

    # --- Save inactive member + pending payment atomically ---
    try:
        with transaction.atomic():
            pool_member = PoolMember.objects.create(
                pool=pool,
                user=request.user,
                role=PoolMember.Role.CONTRIBUTOR,
                payment_status=PoolMember.PaymentStatus.PENDING,
                amount_paid=contribution,
                active=False,
                confirmed_access=False,
                joined_at=None,
            )

            Payment.objects.create(
                pool_member=pool_member,
                user=request.user,
                amount=contribution,            # host share
                copay_fees=total_copay_fees,    # broker's share
                provider="payscrow",
                reference=payscrow_number,
                transaction_guid=transaction_guid,
                status=Payment.Status.PENDING,
            )
    except Exception:
        logger.exception("Database error while saving initialization records")
        return JsonResponse(
            {"success": False, "message": "An internal error occurred. Please try again."},
            status=500,
        )

    return JsonResponse(
        {
            "success": True,
            "payment_link": payment_link,
            "transactionNumber": payscrow_number,
        }
    )


# ---------------------------------------------------------------------------
# 2. VERIFY WITH PAYSCROW
# ---------------------------------------------------------------------------
def verify_with_payscrow(reference):
    """Returns (is_paid, content_dict). Prints everything so you can see what Payscrow says."""
    url = build_status_url(reference)
    headers = {"BrokerApiKey": settings.PAYSCROW_API_KEY, "Content-Type": "application/json"}

    debug_print("[VERIFY] GET", url)
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as e:
        logger.exception("Payscrow verify request failed")
        debug_print("[VERIFY] REQUEST ERROR:", repr(e))
        return False, {}

    debug_print("[VERIFY] HTTP status:", resp.status_code)
    debug_print("[VERIFY] RAW body:", resp.text)

    try:
        body = resp.json()
    except ValueError:
        debug_print("[VERIFY] Response was not JSON")
        return False, {}

    # Payscrow wraps the result in {"data": {...}}
    content = body.get("data") if isinstance(body.get("data"), dict) else body
    if not isinstance(content, dict):
        return False, {}

    # Payscrow returns HTTP 200 even for "Transaction not found", so check its own errors
    if content.get("errors"):
        debug_print("[VERIFY] Payscrow reported errors:", content.get("errors"))
        return False, content

    # The number Payscrow echoes back must be the one we asked about
    returned_number = content.get("transactionNumber")
    if returned_number and returned_number != reference:
        debug_print("[VERIFY] transactionNumber mismatch:", returned_number, "!=", reference)
        return False, content

    status_value = str(content.get("status", "")).replace(" ", "").replace("_", "").lower()
    debug_print("[VERIFY] normalized status:", repr(status_value))
    debug_print("[VERIFY] inEscrow:", content.get("inEscrow"), "| isSuccessful:", content.get("isSuccessful"))

    # Paid = status is a known paid value OR the funds are held in escrow
    paid_by_status = status_value in PAID_STATUSES
    paid_by_escrow = content.get("inEscrow") is True
    is_paid = resp.status_code == 200 and (paid_by_status or paid_by_escrow)

    debug_print("[VERIFY] is_paid:", is_paid)
    return is_paid, content


# ---------------------------------------------------------------------------
# 3. PROCESS PAYMENT (used by both callback and webhook)
# ---------------------------------------------------------------------------
def process_payment(payment_id, webhook_data=None):
    """
    Verify with Payscrow, then activate the member. Safe to call many times.
    Returns a short status string.

    webhook_data: pass the signature-verified webhook payload when called from
    the webhook. A verified "Paid" webhook is trusted, so the extra HTTP verify
    is skipped.
    """
    # Quick check without a lock
    payment = Payment.objects.get(pk=payment_id)
    debug_print("[PROCESS] payment", payment.pk, "ref", payment.reference, "status", payment.status)

    if payment.status == Payment.Status.PAID:
        return "already_processed"

    # Network call happens OUTSIDE the DB lock
    if webhook_data and str(webhook_data.get("paymentStatus", "")).lower() == "paid":
        is_paid, content = True, webhook_data
    else:
        is_paid, content = verify_with_payscrow(payment.reference)

    if not is_paid:
        return "not_paid_yet"

    with transaction.atomic():
        payment = (
            Payment.objects.select_for_update(of=("self",))
            .select_related("pool_member")
            .get(pk=payment_id)
        )

        # Re-check now that we hold the lock (another request may have won)
        if payment.status == Payment.Status.PAID:
            return "already_processed"

        pool_member = payment.pool_member

        # Lock the pool and re-check availability
        pool = Pool.objects.select_for_update().get(pk=pool_member.pool_id)
        now = timezone.now()

        if not pool.active or pool.status != Pool.Status.OPEN:
            logger.error("PAID BUT NEEDS REFUND: %s, pool %s not open", payment.reference, pool.id)
            return "pool_unavailable"

        remaining_days = get_remaining_days(pool, now)
        if remaining_days <= 0:
            logger.error("PAID BUT NEEDS REFUND: %s, pool %s expired", payment.reference, pool.id)
            return "pool_unavailable"

        if pool.current_users >= pool.number_of_users:
            logger.error("PAID BUT NEEDS REFUND: %s, pool %s full", payment.reference, pool.id)
            return "pool_full"

        # Capture the GUID (webhook and verify both call it "transactionId")
        guid = extract_transaction_guid(content) or content.get("id")
        debug_print("[PROCESS] GUID:", guid)
        attach_guid(payment, guid)

        payment.status = Payment.Status.PAID
        payment.paid_at = now
        payment.stint_started_at = now
        payment.save(update_fields=["status", "paid_at", "stint_started_at", "transaction_guid"])
        debug_print("[PROCESS] payment marked PAID")

        # Same user already active in this pool: keep the payment record, don't take a second slot
        already_active = (
            PoolMember.objects.filter(pool=pool, user=pool_member.user, active=True)
            .exclude(pk=pool_member.pk)
            .exists()
        )
        if already_active:
            debug_print("[PROCESS] user already active in this pool; no new slot")
            return "already_member"

        if not pool_member.active:
            pool_member.active = True
            pool_member.payment_status = PoolMember.PaymentStatus.PAID
            pool_member.joined_at = now
            pool_member.save(update_fields=["active", "payment_status", "joined_at"])
            debug_print("[PROCESS] member ACTIVATED")

            Notification.objects.create(
                user=pool.host,
                pool=pool,
                type=Notification.Type.POOL,
                title="New Pool Member Joined",
                description=f"@{pool_member.user.username} has successfully paid and joined your {pool.service.name} Shared Pool.",
                url=f"/market/{pool.pk}/",
            )
            debug_print("[PROCESS] notification created")

        return "success"


# ---------------------------------------------------------------------------
# 4. CALLBACK: user returns from Payscrow. Verifies + activates.
# ---------------------------------------------------------------------------
@login_required
def payscrow_payment_callback(request, pool_id):
    pool = get_object_or_404(Pool, id=pool_id)

    debug_print("========== PAYSCROW CALLBACK ==========")
    debug_print("GET params from Payscrow:", dict(request.GET))
    debug_print("user:", request.user.id, "pool:", pool.id)

    # Never trust the URL for the reference. Use OUR record for THIS user + pool.
    payment = (
        Payment.objects
        .filter(user=request.user, pool_member__pool=pool)
        .exclude(status=Payment.Status.PAID)
        .order_by("-id")
        .first()
    )

    if payment is None:
        already = PoolMember.objects.filter(pool=pool, user=request.user, active=True).exists()
        debug_print("[CALLBACK] no unpaid payment found. already active:", already)
        if already:
            messages.success(request, "Payment verified! Welcome to the pool.")
        else:
            messages.error(request, "No pending payment found for this pool.")
        return redirect("market_detail", pk=pool.pk)

    try:
        result = process_payment(payment.pk)
    except Exception:
        logger.exception("Error processing payment %s in callback", payment.pk)
        debug_print("[CALLBACK] EXCEPTION, see traceback in logs")
        result = "error"

    debug_print("[CALLBACK] result:", result)

    if result in ("success", "already_processed", "already_member"):
        messages.success(request, "Payment verified! Welcome to the pool.")
    elif result == "not_paid_yet":
        messages.info(request, "We haven't received confirmation of your payment yet. Please refresh in a moment.")
    elif result in ("pool_full", "pool_unavailable"):
        messages.warning(request, "Your payment was received but the pool is no longer available. Please contact support.")
    else:
        messages.error(request, "We couldn't confirm your payment automatically. Please contact support with your reference.")

    return redirect("market_detail", pk=pool.pk)


# ---------------------------------------------------------------------------
# 5. WEBHOOK: Payscrow server -> your server (signed with settings.X_PAYSCROW)
# ---------------------------------------------------------------------------
def _signature_ok(request):
    """
    Verifies X-PayScrow-Signature = sha256=HMAC_SHA256(secret, "{timestamp}.{rawBody}")
    and rejects timestamps older than 5 minutes.
    """
    secret = getattr(settings, "X_PAYSCROW", "")
    ts = request.headers.get("X-PayScrow-Timestamp", "")
    sig = request.headers.get("X-PayScrow-Signature", "")

    if not (secret and ts and sig):
        return False

    try:
        if abs(time.time() - int(ts)) > 300:
            return False
    except ValueError:
        return False

    expected = "sha256=" + hmac.new(
        secret.encode(),
        f"{ts}.".encode() + request.body,   # raw bytes, not re-serialized
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(sig, expected)


@csrf_exempt
def payscrow_webhook(request):
    debug_print("========== PAYSCROW WEBHOOK HIT ==========")
    debug_print("method:", request.method, "body:", request.body)

    if request.method != "POST":
        return HttpResponse(status=405)

    if not _signature_ok(request):
        logger.warning("Payscrow webhook: bad/missing signature or stale timestamp")
        debug_print("[WEBHOOK] signature check FAILED")
        return JsonResponse({"status": "invalid signature"}, status=401)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"status": "Invalid JSON"}, status=400)

    if not isinstance(data, dict):
        return JsonResponse({"status": "Invalid payload"}, status=400)

    number = data.get("transactionNumber")
    external = data.get("externalReference")

    payment = None
    if number:
        payment = Payment.objects.filter(reference=number).first()
    if payment is None and external:
        payment = Payment.objects.filter(reference=external).first()

    if payment is None:
        logger.warning("Webhook for unknown payment: %s / %s", number, external)
        debug_print("[WEBHOOK] unknown reference:", number, external)
        # Acknowledge so Payscrow stops retrying
        return JsonResponse({"received": True})

    # Always save the GUID first
    if attach_guid(payment, data.get("transactionId")):
        payment.save(update_fields=["transaction_guid"])
        debug_print("[WEBHOOK] saved GUID:", payment.transaction_guid)

    # Dispute events: mirror the state and stop
    if data.get("event") == "dispute.status_changed":
        payment.payscrow_dispute_raised = bool(data.get("inDispute"))
        payment.save(update_fields=["payscrow_dispute_raised"])
        return JsonResponse({"received": True})

    # Transaction paid event
    try:
        result = process_payment(payment.pk, webhook_data=data)
    except Exception:
        logger.exception("Error processing payment %s in webhook", payment.pk)
        return JsonResponse({"status": "error"}, status=500)  # Payscrow will retry

    debug_print("[WEBHOOK] result:", result)
    return JsonResponse({"received": True, "status": result}, status=200)





@admin_only
@require_complete_site_info
def admin_page(request):
    
    now = timezone.now()
    start_of_this_week = now - timedelta(days=7)
    start_of_last_week = now - timedelta(days=14)
    start_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Real users only: must have a UserProfile, and must not be an admin account.
    # (To ignore only superusers, delete the is_staff line.)
    real_users = User.objects.filter(
        profile__isnull=False,
        is_superuser=False,
        is_staff=False,
    )

    # Base queryset for paid payments
    paid_payments = Payment.objects.filter(status=Payment.Status.PAID)

    # Calculate Revenue Metrics from the Payment model
    revenue_stats = paid_payments.aggregate(
        total_platform_revenue=Coalesce(
            Sum(F('amount') + Coalesce('copay_fees', Value(0, output_field=DecimalField()))),
            Value(0, output_field=DecimalField())
        ),
        total_copay_fees=Coalesce(
            Sum('copay_fees'),
            Value(0, output_field=DecimalField())
        ),
        this_month_revenue=Coalesce(
            Sum(
                F('amount') + Coalesce('copay_fees', Value(0, output_field=DecimalField())),
                filter=Q(created_at__gte=start_of_this_month)
            ),
            Value(0, output_field=DecimalField())
        )
    )

    # 1. General Overview Metrics
    stats_data = {
        "totalUsers": real_users.count(),
        "membersThisWeek": PoolMember.objects.filter(
            joined_at__gte=start_of_this_week
        ).count(),
        "membersLastWeek": PoolMember.objects.filter(
            joined_at__gte=start_of_last_week, joined_at__lt=start_of_this_week
        ).count(),
        "totalPools": Pool.objects.count(),
        "activePools": Pool.objects.filter(status=Pool.Status.OPEN).count(),
        "completedPools": Pool.objects.filter(
            status=Pool.Status.COMPLETED
        ).count(),
        "cancelledPools": Pool.objects.filter(
            status=Pool.Status.CANCELLED
        ).count(),
        "openDisputes": Dispute.objects.filter(
            status=Dispute.Status.OPEN
        ).count(),
        # Financial metrics for the balance board
        "totalPlatformRevenue": float(revenue_stats["total_platform_revenue"]),
        "totalCopayFees": float(revenue_stats["total_copay_fees"]),
        "thisMonthRevenue": float(revenue_stats["this_month_revenue"]),
    }

    # 2. Users Directory Data (same real_users queryset as the count above)
    users_data = []
    for u in (
        real_users
        .select_related("profile")
        .annotate(
            total_pools=Count("hosted_pools"),
            successful_pools=Count(
                "hosted_pools",
                filter=Q(hosted_pools__status=Pool.Status.COMPLETED),
            ),
        )
        .order_by("-date_joined")
    ):
        total = u.total_pools
        success = u.successful_pools
        success_rate = round((success / total * 100), 1) if total > 0 else 0.0
        users_data.append({
            "id": u.profile.pk,
            "name": (
                f"{u.first_name} {u.last_name}".strip()
                if u.first_name or u.last_name
                else u.username
            ),
            "email": u.email or "No email",
            "phone": u.profile.phone_number or "N/A",
            "joined": (
                u.date_joined.strftime("%b %d, %Y") if u.date_joined else "N/A"
            ),
            "totalPools": total,
            "successRate": success_rate,
        })

    # 3. Pools Directory Data
    pools_data = []
    for p in (
        Pool.objects.select_related("host", "service")
        .annotate(
            active_members=Count("members", filter=Q(members__active=True))
        )
        .order_by("-created_at")
    ):
        host_name = (
            f"{p.host.first_name} {p.host.last_name}".strip()
            if p.host.first_name or p.host.last_name
            else p.host.username
        )
        pools_data.append({
            "id": p.id,
            "service": p.service.name if p.service else "Unknown Service",
            "host": host_name,
            "isPublic": p.is_public,
            "activeMembers": p.active_members,
            "maxUsers": p.number_of_users,
            "status": p.status,
        })

    # 4. Disputes Directory Data
    disputes_data = []
    for d in Dispute.objects.select_related(
        "pool", "pool__service", "opened_by", "against_user"
    ).order_by("-created_at"):
        pool_title = (
            f"{d.pool.service.name} Pool" if d.pool and d.pool.service else "Pool"
        )
        disputes_data.append({
            "id": d.id,
            "name": d.name or f"Dispute #{d.id}",
            "reason": d.reason or "No reason provided",
            "poolName": pool_title,
            "openedBy": f"@{d.opened_by.username}" if d.opened_by else "@unknown",
            "againstUser": (
                f"@{d.against_user.username}" if d.against_user else "@unknown"
            ),
            "status": d.status,
        })

    # 5. Payment History Ledger Data
    payments_data = []
    for ph in PaymentHistory.objects.select_related(
        "user", "pool", "pool__service"
    ).order_by("-created_at"):
        pool_name = (
            ph.pool.service.name
            if ph.pool and ph.pool.service
            else ("General Pool" if ph.pool else "N/A")
        )
        payments_data.append({
            "id": ph.id,
            "reference": ph.payment_reference or f"TX-{ph.id}",
            "username": f"@{ph.user.username}" if ph.user else "@unknown",
            "pool": pool_name,
            "kind": ph.get_kind_display(),
            "kindValue": ph.kind,
            "amount": float(ph.amount),
            "status": "Completed",
        })

    # 6. Services Directory Data (Only Standard types)
    services_data = []
    for s in Service.objects.filter(type=Service.Type.STANDARD).order_by("name"):
        services_data.append({
            "id": s.id,
            "name": s.name,
            "category": s.get_category_display(),
            "maxPeople": s.max_people,
            "standardPrice": float(s.standard_price),
        })

    # Context to pass to template
    context = {
        "stats_json": json.dumps(stats_data),
        "users_json": json.dumps(users_data),
        "pools_json": json.dumps(pools_data),
        "disputes_json": json.dumps(disputes_data),
        "payments_json": json.dumps(payments_data),
        "services_json": json.dumps(services_data),
    }
    return render(request, "admin_base.html", context)


def user_profile(request, pk):
    profile = get_object_or_404(
        UserProfile.objects.select_related("user"), pk=pk
    )
    user = profile.user

    # 1. Reputation & Host Ratings Calculations
    reputation, _ = Reputation.objects.get_or_create(user=user)
    ratings = Rating.objects.filter(host=user)
    total_ratings_count = ratings.count()
    avg_rating = (
        ratings.aggregate(models.Avg("rating"))["rating__avg"] or Decimal("0.00")
    )

    # Success Rate: (Successful pools hosted by him / Total pools created) * 100
    total_hosted_pools = Pool.objects.filter(host=user).count()
    successful_hosted_pools = Pool.objects.filter(
        host=user, status=Pool.Status.COMPLETED
    ).count()
    success_rate = (
        round((successful_hosted_pools / total_hosted_pools) * 100, 1)
        if total_hosted_pools > 0
        else 0.0
    )

    # Total earnings: sum of all payment history amounts where kind is earning (exclude refund)
    total_earnings = (
        PaymentHistory.objects.filter(
            user=user, kind=PaymentHistory.Kind.EARNING
        ).aggregate(models.Sum("amount"))["amount__sum"]
        or Decimal("0.00")
    )

    # 2. Pools Created by User Data
    created_pools_data = []
    for p in (
        Pool.objects.filter(host=user)
        .select_related("service")
        .annotate(
            active_members=Count("members", filter=Q(members__active=True))
        )
        .order_by("-created_at")
    ):
        created_pools_data.append({
            "id": p.id,
            "service": p.service.name if p.service else "Unknown Service",
            "is_public": p.is_public,
            "active_members": p.active_members,
            "number_of_users": p.number_of_users,
            "status": p.status,
        })

    # 3. Pools Joined by User Data (Active and Inactive memberships)
    joined_pools_data = []
    memberships = (
        PoolMember.objects.filter(user=user)
        .select_related("pool", "pool__service", "pool__host")
        .order_by("-joined_at")
    )
    for m in memberships:
        p = m.pool
        active_members_count = p.members.filter(active=True).count()
        host_name = (
            f"{p.host.first_name} {p.host.last_name}".strip()
            if p.host.first_name or p.host.last_name
            else p.host.username
        )
        joined_pools_data.append({
            "id": p.id,
            "service": p.service.name if p.service else "Unknown Service",
            "host": f"@{host_name}",
            "active_members": (
                0 if p.status == Pool.Status.CANCELLED else active_members_count
            ),
            "number_of_users": p.number_of_users,
            "is_active_membership": m.active,
            "status": p.status,
        })

    # 4. Disputes Involving User (Opened by or Against)
    user_disputes = (
        Dispute.objects.filter(
            Q(opened_by=user) | Q(against_user=user)
        )
        .select_related("pool", "pool__service", "opened_by", "against_user")
        .order_by("-created_at")
    )

    # Metrics count: Check only disputes where against_user is the user
    resolved_disputes = user_disputes.filter(
        against_user=user, status=Dispute.Status.RESOLVED
    ).count()
    unresolved_disputes = user_disputes.filter(
        against_user=user, status=Dispute.Status.OPEN
    ).count()

    disputes_data = []
    for d in user_disputes:
        pool_title = (
            f"{d.pool.service.name}" if d.pool and d.pool.service else "Pool"
        )

        def get_user_display(u):
            if not u:
                return "Unknown"
            name = f"{u.first_name} {u.last_name}".strip()
            return f"{name} (@{u.username})" if name else f"@{u.username}"

        disputes_data.append({
            "id": d.id,
            "name": d.name or f"Dispute #{d.id}",
            "pool": pool_title,
            "opened_by": get_user_display(d.opened_by),
            "against_user": get_user_display(d.against_user),
            "status": d.status,
        })

    # 5. Payment History Related to User
    payments_data = []
    user_payments = (
        PaymentHistory.objects.filter(user=user)
        .select_related("pool", "pool__service")
        .order_by("-created_at")
    )
    for ph in user_payments:
        pool_name = (
            ph.pool.service.name
            if ph.pool and ph.pool.service
            else ("General Pool" if ph.pool else "N/A")
        )
        payments_data.append({
            "id": ph.id,
            "reference": ph.payment_reference or f"TX-{ph.id}",
            "pool": pool_name,
            "kind": ph.get_kind_display(),
            "kind_value": ph.kind,
            "amount": float(ph.amount),
        })

    context = {
        "profile": profile,
        "user_obj": user,
        "avg_rating": round(avg_rating, 2),
        "total_ratings_count": total_ratings_count,
        "success_rate": success_rate,
        "total_earnings": total_earnings,
        "successful_hosted_pools": successful_hosted_pools,
        "resolved_disputes": resolved_disputes,
        "unresolved_disputes": unresolved_disputes,
        "created_pools": created_pools_data,
        "joined_pools": joined_pools_data,
        "disputes": disputes_data,
        "payments": payments_data,
    }
    return render(request, "user_profile.html", context)



@admin_only
def service_detail(request, pk):
  service = get_object_or_404(Service, pk=pk)

  # Handle Form Submissions (Edit or Delete)
  if request.method == "POST":
    action = request.POST.get("action")

    # Handle Service Deletion
    if action == "delete_service":
      service_name = service.name
      service.delete()
      messages.success(request, f"Service '{service_name}' deleted successfully.")
      # Change 'services_list' to whatever your services index/dashboard url name is
      return redirect("admin_page")

    # Handle Service Edit (default fallback or action == 'edit_service')
    name = request.POST.get("name")
    category = request.POST.get("category")
    standard_price = request.POST.get("standard_price")
    max_people = request.POST.get("max_people")
    image = request.FILES.get("image")

    if name:
      service.name = name
    if category:
      service.category = category
    if standard_price:
      service.standard_price = Decimal(standard_price)
    if max_people:
      service.max_people = int(max_people)
    if image:
      service.image = image

    service.save()
    messages.success(request, "Service updated successfully.")
    return redirect("service_detail", pk=service.pk)

  # Fetch all pools linked to this service with active member counts
  pools_data = []
  pools = (
      Pool.objects.filter(service=service)
      .select_related("host")
      .annotate(
          active_members_count=Count(
              "members", filter=Q(members__active=True), distinct=True
          )
      )
      .order_by("-created_at")
  )
  for p in pools:
    host_name = (
        f"{p.host.first_name} {p.host.last_name}".strip()
        if p.host.first_name or p.host.last_name
        else p.host.username
    )
    pools_data.append({
        "id": p.id,
        "host": (
            f"@{host_name}"
            if not (p.host.first_name or p.host.last_name)
            else host_name
        ),
        "is_public": p.is_public,
        "active_members": (
            0 if p.status == Pool.Status.CANCELLED else p.active_members_count
        ),
        "number_of_users": p.number_of_users,
        "status": p.status,
    })

  context = {
      "service": service,
      "pools": pools_data,
  }
  return render(request, "service_detail.html", context)




def get_site_info():
    """There should only ever be one SiteInfo row, regardless of its pk.
    Get it if it exists; only create one if the table is genuinely empty."""
    site_info = SiteInfo.objects.first()
    if site_info is None:
        site_info = SiteInfo.objects.create()
    return site_info


@login_required
@admin_only
def settings_view(request):
    site_info = get_site_info()
    return render(request, "admin_settings.html", {"site_info": site_info})


@login_required
@require_POST
def update_site_info(request):
    """Updates ONE section of SiteInfo at a time: contact / pricing / bank."""
    try:
        body = json.loads(request.body)
        section = body.get("section")
        site_info = get_site_info()
        
        if section == "contact":
            phone = body.get("phone", "").strip()
            email = body.get("email", "").strip()

            if not phone or not email:
                return JsonResponse({"status": False, "message": "Phone and email are required."}, status=400)

            if not re.match(r"^234\d{10}$", phone):
                return JsonResponse({"status": False, "message": "Phone must start with '234' followed by exactly 10 digits."}, status=400)

            site_info.phone = phone
            site_info.email = email
            site_info.save(update_fields=["phone", "email"])
            return JsonResponse({"status": True, "data": {"phone": site_info.phone, "email": site_info.email}})

        elif section == "pricing":
            percentage_value = str(body.get("site_percentage", "")).strip()
            price_value = str(body.get("price", "")).replace(",", "").strip()

            if not percentage_value and not price_value:
                return JsonResponse({
                    "status": False,
                    "message": "Enter either a percentage or a price."
                }, status=400)

            try:
                site_percentage = (
                    Decimal(percentage_value)
                    if percentage_value
                    else None
                )
                price = (
                    Decimal(price_value)
                    if price_value
                    else None
                )
            except (InvalidOperation, TypeError):
                return JsonResponse({
                    "status": False,
                    "message": "Enter valid numbers for percentage or price."
                }, status=400)

            if site_percentage is not None:
                if site_percentage < 0 or site_percentage > 100:
                    return JsonResponse({
                        "status": False,
                        "message": "Percentage must be between 0 and 100."
                    }, status=400)

            if price is not None:
                if price < 0:
                    return JsonResponse({
                        "status": False,
                        "message": "Price cannot be less than 0."
                    }, status=400)

            site_info.site_percentage = site_percentage
            site_info.price = price
            site_info.save(update_fields=["site_percentage", "price"])

            return JsonResponse({
                "status": True,
                "data": {
                    "site_percentage": (
                        str(site_info.site_percentage)
                        if site_info.site_percentage is not None
                        else None
                    ),
                    "price": (
                        str(site_info.price)
                        if site_info.price is not None
                        else None
                    ),
                },
            })

        

        elif section == "bank":
            # Bank details can only be saved once resolve_site_bank has
            # actually verified them with Paystack this session — this
            # endpoint never accepts raw bank fields directly.
            verified = request.session.get("verified_site_bank")
            if not verified:
                return JsonResponse({"status": False, "message": "Please verify the account with Paystack before saving."}, status=400)

            site_info.bank = verified["bank_name"]
            site_info.bank_code = verified["bank_code"]
            site_info.account_number = verified["account_number"]
            site_info.account_name = verified["account_name"]
            site_info.save(update_fields=["bank", "bank_code", "account_number", "account_name"])

            del request.session["verified_site_bank"]

            return JsonResponse({
                "status": True,
                "data": {
                    "bank": site_info.bank,
                    "account_number": site_info.account_number,
                    "account_name": site_info.account_name,
                },
            })

        return JsonResponse({"status": False, "message": "Unknown section."}, status=400)

    except Exception as e:
        return JsonResponse({"status": False, "message": str(e)}, status=500)
    

@login_required
@require_POST
def resolve_site_bank(request):
    """
    Same two-purpose endpoint pattern as your existing user resolve_bank:
      - called WITHOUT account_name -> just verifies + returns account_name (preview)
      - called WITH bank_name + account_name -> actually saves to SiteInfo
    """
    try:
        body = json.loads(request.body)
        account_number = body.get("account_number", "").strip()
        bank_code = body.get("bank_code", "").strip()
        bank_name = body.get("bank_name", "").strip()
        account_name = body.get("account_name", "").strip()

        if not account_number or len(account_number) != 10:
            return JsonResponse({"status": False, "message": "A valid 10-digit account number is required."}, status=400)
        if not bank_code:
            return JsonResponse({"status": False, "message": "Please select a valid bank from the list."}, status=400)

        # Preview call: no account_name yet, so verify with Paystack
        if not account_name:
            response = requests.get(
                "https://api.paystack.co/bank/resolve",
                headers={"Authorization": f"Bearer {django_settings.PAYSTACK_SECRET_KEY}"},
                params={"account_number": account_number, "bank_code": bank_code},
                timeout=15,
            )
            data = response.json()
            if not data.get("status"):
                return JsonResponse({"status": False, "message": data.get("message", "Account does not exist or invalid details.")}, status=400)
            return JsonResponse({"status": True, "account_name": data["data"]["account_name"]})

        # Save call: bank_name + account_name are both present -> commit to SiteInfo
        site_info = get_site_info()
        site_info.bank = bank_name
        site_info.bank_code = bank_code
        site_info.account_number = account_number
        site_info.account_name = account_name
        site_info.save(update_fields=["bank", "bank_code", "account_number", "account_name"])

        return JsonResponse({
            "status": True,
            "message": "Account linked successfully!",
            "data": {
                "bank": site_info.bank,
                "account_number": site_info.account_number,
                "account_name": site_info.account_name,
            },
        })

    except Exception as e:
        return JsonResponse({"status": False, "message": str(e)}, status=500)



@login_required
@require_POST
def change_password_view(request):
    try:
        body = json.loads(request.body)
        old_password = body.get("old_password", "")
        new_password = body.get("new_password", "")

        if not old_password or not new_password:
            return JsonResponse({"status": False, "message": "Both fields are required."}, status=400)

        if not request.user.check_password(old_password):
            return JsonResponse({"status": False, "message": "Current password is incorrect."}, status=400)

        if len(new_password) < 8 or not any(c.isdigit() for c in new_password):
            return JsonResponse({"status": False, "message": "New password must be at least 8 characters and contain a number."}, status=400)

        request.user.set_password(new_password)
        request.user.save()
        update_session_auth_hash(request, request.user)  # keeps them logged in

        return JsonResponse({"status": True, "message": "Password changed successfully."})

    except Exception as e:
        return JsonResponse({"status": False, "message": str(e)}, status=500)
    
    



@login_required
def service(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        category = request.POST.get("category", "").strip()
        standard_price = request.POST.get("standard_price", "").strip()
        max_people = request.POST.get("max_people", "").strip()
        image = request.FILES.get("image")

        # --- Validation ---
        if not name or not category or not standard_price or not max_people:
            messages.error(request, "All fields are required except the image.")
            return render(request, "service.html", {"categories": Service.Category.choices})

        if category not in Service.Category.values:
            messages.error(request, "Please select a valid category.")
            return render(request, "service.html", {"categories": Service.Category.choices})

        try:
            standard_price = Decimal(standard_price)
            if standard_price <= 0:
                raise InvalidOperation
        except InvalidOperation:
            messages.error(request, "Enter a valid price greater than 0.")
            return render(request, "service.html", {"categories": Service.Category.choices})

        try:
            max_people = int(max_people)
            if max_people < 1:
                raise ValueError
        except ValueError:
            messages.error(request, "Max people must be a whole number of at least 1.")
            return render(request, "service.html", {"categories": Service.Category.choices})

        if Service.objects.filter(name__iexact=name).exists():
            messages.error(request, "A service with this name already exists.")
            return render(request, "service.html", {"categories": Service.Category.choices})

        Service.objects.create(
            name=name,
            category=category,
            type=Service.Type.STANDARD,
            standard_price=standard_price,
            max_people=max_people,
            image=image,
        )

        messages.success(request, f"'{name}' was published successfully.")
        return redirect("admin_page")

    return render(request, "service.html", {"categories": Service.Category.choices})




logger = logging.getLogger(__name__)

PAYSCROW_APPLYCODE_URL = (
    "https://api.payscrow.net/api/v3/escrow/escrowtransactions/applycode"
)


@login_required
def release_escrow_view(request, pk):
    member = get_object_or_404(
        PoolMember,
        pk=pk,
        user=request.user,
    )

    payment = member.payments.order_by("-id").first()

    if not payment:
        messages.error(
            request,
            "No payment found for this pool member."
        )
        return redirect("dashboard")

    if request.method == "POST":

        transaction_guid = request.POST.get(
            "transaction_guid",
            ""
        ).strip()

        escrow_code = request.POST.get(
            "escrow_code",
            ""
        ).strip()

        if not transaction_guid:
            messages.error(
                request,
                "Enter the PayScrow transaction ID."
            )

            return render(
                request,
                "release_escrow.html",
                {
                    "member": member,
                    "payment": payment,
                },
            )

        if not escrow_code:
            messages.error(
                request,
                "Enter the escrow code."
            )

            return render(
                request,
                "release_escrow.html",
                {
                    "member": member,
                    "payment": payment,
                },
            )

        headers = {
            "BrokerApiKey": settings.PAYSCROW_API_KEY,
            "Content-Type": "application/json",
        }


        payload = {
            "transactionId": transaction_guid,  # <-- Change this from "Transaction Guid"
            "code": escrow_code,                # <-- Change this to lowercase "code"
        }

        print("PAYSCROW APPLY CODE PAYLOAD:", payload)

        try:
            response = requests.post(
                PAYSCROW_APPLYCODE_URL,
                json=payload,
                headers=headers,
                timeout=30,
            )

            try:
                data = response.json()
            except ValueError:
                data = {}

            print(
                "PAYSCROW APPLY CODE RESPONSE:",
                data
            )

        except requests.RequestException as e:
            print(
                "PAYSCROW ERROR:",
                e
            )

            messages.error(
                request,
                "Could not connect to PayScrow."
            )

            return render(
                request,
                "release_escrow.html",
                {
                    "member": member,
                    "payment": payment,
                },
            )

        if response.ok and data.get("success"):

            member.confirmed_access = True
            member.confirmed_at = timezone.now()

            member.save(
                update_fields=[
                    "confirmed_access",
                    "confirmed_at",
                ]
            )

            messages.success(
                request,
                "Escrow code accepted and payment released."
            )

            return redirect("dashboard")

        error_message = (
            data.get("message")
            or "PayScrow rejected the transaction."
        )

        if data.get("errors"):
            errors = data["errors"]

            if isinstance(errors, list):
                error_message = " | ".join(
                    str(error)
                    for error in errors
                )
            else:
                error_message = str(errors)

        messages.error(
            request,
            error_message
        )

    return render(
        request,
        "release_escrow.html",
        {
            "member": member,
            "payment": payment,
        },
    )
    


@login_required(login_url="signin")
def pool_calculation_breakdown_api(request, pk):
    pool = get_object_or_404(Pool, pk=pk)
    
    # Validate that the pool is active and status is open
    if not pool.active or pool.status != Pool.Status.OPEN:
        return JsonResponse({
            "success": False, 
            "error": "This pool is not currently open for joining."
        }, status=400)

    site_info = SiteInfo.objects.first()
    two_places = Decimal("0.01")
    
    # Actual slot price per person: pool price / number of users
    amount_per_person = pool.amount_per_person.quantize(two_places, rounding=ROUND_HALF_UP)

    site_percentage = site_info.site_percentage if site_info and site_info.site_percentage else Decimal("0.00")
    flat_price = site_info.price if site_info and site_info.price else Decimal("0.00")
    
    copay_percentage_fee = (amount_per_person * (site_percentage / Decimal("100.00"))).quantize(two_places, rounding=ROUND_HALF_UP)
    copay_flat_fee = flat_price.quantize(two_places, rounding=ROUND_HALF_UP)
    
    calculated_copay_total = (amount_per_person + copay_percentage_fee + copay_flat_fee).quantize(two_places, rounding=ROUND_HALF_UP)

    return JsonResponse({
        "success": True,
        "amount_per_person": float(amount_per_person),
        "site_percentage": float(site_percentage),
        "copay_percentage_fee": float(copay_percentage_fee),
        "copay_flat_fee": float(copay_flat_fee),
        "calculated_copay_total": float(calculated_copay_total),
    })   
    


PAYSCROW_STATUS_URL = (
    "https://api.payscrow.net/api/v3/marketplace/transactions/{number}/status"
)


def _find_status(data):
    """
    Payscrow's response shape isn't confirmed yet, so look for a status in
    the usual places. Once you paste me a real response we can make this exact.
    """
    if not isinstance(data, dict):
        return None

    for container in (data, data.get("data"), data.get("result")):
        if isinstance(container, dict):
            for key in ("status", "transactionStatus", "state", "escrowStatus"):
                if container.get(key):
                    return str(container[key])
    return None


@login_required
def check_escrow_status_view(request, pk):
    member = get_object_or_404(
        PoolMember.objects.select_related("pool", "user"),
        Q(user=request.user) | Q(pool__host=request.user),
        pk=pk,
    )

    payment = member.payments.order_by("-id").first()

    # 1) a number typed into the page wins, 2) otherwise use what we stored
    typed_number = request.GET.get("transaction_number", "").strip()
    stored_number = ""
    if payment:
        stored_number = payment.transaction_guid or payment.reference or ""

    transaction_number = typed_number or stored_number

    context = {
        "member": member,
        "payment": payment,
        "transaction_number": transaction_number,
        "used_stored_number": bool(transaction_number) and not typed_number,
        "status": None,
        "error": None,
        "raw_json": None,
        "http_status": None,
    }

    if not transaction_number:
        context["error"] = "No transaction number yet. Enter it below."
        return render(request, "escrow_status.html", context)

    url = PAYSCROW_STATUS_URL.format(number=quote(transaction_number, safe=""))

    headers = {
        "BrokerApiKey": settings.PAYSCROW_API_KEY,
        "Content-Type": "application/json",
    }

    print("PAYSCROW STATUS URL:", url)

    try:
        response = requests.get(url, headers=headers, timeout=30)

        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text}

        print("PAYSCROW STATUS HTTP:", response.status_code)
        print("PAYSCROW STATUS RESPONSE:", data)

    except requests.RequestException as e:
        print("PAYSCROW STATUS ERROR:", e)
        context["error"] = "Could not connect to PayScrow."
        return render(request, "escrow_status.html", context)

    context["http_status"] = response.status_code
    context["raw_json"] = json.dumps(data, indent=2, default=str)
    context["status"] = _find_status(data)

    if not response.ok:
        context["error"] = (
            data.get("message") if isinstance(data, dict) else None
        ) or f"PayScrow returned HTTP {response.status_code}."

    return render(request, "escrow_status.html", context)



@login_required(login_url="signin")
@require_POST
def save_pool_escrow_code(request, pk):
    pool = get_object_or_404(Pool, pk=pk)
    
    try:
        data = json.loads(request.body)
        escrow_code = data.get("escrow_code", "").strip()
    except json.JSONDecodeError:
        return JsonResponse({"status": "error", "message": "Invalid JSON payload."}, status=400)
    
    if not escrow_code or len(escrow_code) != 6:
        return JsonResponse({"status": "error", "message": "A valid 6-character escrow code is required."}, status=400)
    
    # Get active pool membership for the current user
    membership = PoolMember.objects.filter(pool=pool, user=request.user, active=True).first()
    if not membership:
        return JsonResponse({"status": "error", "message": "Active pool membership not found for this user."}, status=403)
    
    # Retrieve or filter the payment entry matching pool_member and user
    payment = Payment.objects.filter(pool_member=membership, user=request.user).first()
    
    if not payment:
        # Fallback search if pool_member wasn't explicitly linked yet but payment matches user and pool amount/reference context
        payment = Payment.objects.filter(user=request.user, status=Payment.Status.PAID).last()
        if payment and not payment.pool_member:
            payment.pool_member = membership
            payment.save()
            
    if payment:
        # Update or set the escrow code field (noting model spelling: ecrow_code or escrow_code based on your definition)
        payment.ecrow_code = escrow_code.upper()
        payment.save()
    else:
        # Create a blank/new payment record skeleton if absolutely necessary, 
        # though normally payscrow triggers should have created this already.
        Payment.objects.create(
            pool_member=membership,
            user=request.user,
            amount=pool.amount_per_person,
            ecrow_code=escrow_code.upper(),
            status=Payment.Status.PAID
        )
        
    return JsonResponse({
        "status": "success", 
        "message": "Escrow code saved successfully! Finalizing setup..."
    })
    

def is_copay_admin(user):
    """Superuser, staff, or a member of the Django group called 'admin'."""
    if not user.is_authenticated:
        return False
 
    return bool(
        user.is_superuser
        or user.is_staff
        or user.groups.filter(name__iexact="admin").exists()
    )
 
 
def _forbidden():
    return JsonResponse(
        {"success": False, "error": "Only CoPay admins can do this."},
        status=403,
    )
 
 
# ---------------------------------------------------------------------------
# REMOVE A MEMBER (admin only)  -- replaces leave_pool_ajax
# ---------------------------------------------------------------------------
@login_required(login_url="signin")
@require_POST
def remove_member_ajax(request, pk, member_pk):
    if not is_copay_admin(request.user):
        return _forbidden()
 
    pool = get_object_or_404(Pool, pk=pk)
 
    member = (
        PoolMember.objects
        .select_related("user")
        .filter(pk=member_pk, pool=pool, active=True)
        .first()
    )
 
    print("========== ADMIN REMOVE MEMBER ==========")
    print("admin:", request.user.username, "| pool:", pool.pk, "| member found:", bool(member))
 
    if not member:
        return JsonResponse(
            {"success": False, "error": "Active member not found in this pool."},
            status=400,
        )
 
    now = timezone.now()
 
    # 1. Remove the member and free their slot
    with transaction.atomic():
        member.left_at = now
        member.active = False
        member.confirmed_access = False
        member.confirmed_at = None
        member.save(update_fields=["left_at", "active", "confirmed_access", "confirmed_at"])
        print(f"[REMOVE] {member.user.username} deactivated, left_at={now}")
 
        current_users = pool.members.filter(active=True).count()
 
        if pool.status == Pool.Status.CLOSE and current_users < pool.number_of_users:
            pool.status = Pool.Status.OPEN
            pool.save(update_fields=["status"])
            print("[REMOVE] pool reopened, slot available")
 
    # 2. Payscrow dispute for this member's payment(s)
    print("[REMOVE] creating payscrow dispute for the removed member")
    results = raise_disputes_for_member(member)
    failed = [r for r in results if not r.get("ok")]
 
    # 3. Notifications
    if results and not failed:
        member_text = (
            f"You have been removed from the {pool.service.name} Shared Pool "
            f"by CoPay support. A dispute has been opened with our payment "
            f"partner to request a partial refund for the period you did not use."
        )
    elif results:
        member_text = (
            f"You have been removed from the {pool.service.name} Shared Pool "
            f"by CoPay support. Our team is reviewing your refund and will contact you."
        )
    else:
        member_text = (
            f"You have been removed from the {pool.service.name} Shared Pool "
            f"by CoPay support."
        )
 
    Notification.objects.create(
        user=member.user,
        pool=pool,
        type=Notification.Type.POOL,
        title="Removed From Pool",
        description=member_text,
        url=f"/market/{pool.pk}/",
    )
 
    Notification.objects.create(
        user=pool.host,
        pool=pool,
        type=Notification.Type.POOL,
        title="Contributor Removed",
        description=(
            f"@{member.user.username} was removed from your {pool.service.name} "
            f"Shared Pool by CoPay support. Slot capacity is now "
            f"{current_users}/{pool.number_of_users} filled."
        ),
        url=f"/market/{pool.pk}/",
    )
 
    message = f"@{member.user.username} was removed and the member was notified."
    if not results:
        message += " They had no paid payment, so no dispute was needed."
    elif failed:
        message += (
            f" WARNING: the Payscrow dispute could not be created for "
            f"{len(failed)} payment(s). Check the terminal and raise it by hand."
        )
    else:
        message += " A Payscrow dispute was created."
 
    return JsonResponse({"success": True, "message": message, "dispute_results": results})
 
 
# ---------------------------------------------------------------------------
# CANCEL A POOL (admin only)
#
# Setting the status to CANCELLED is all this view has to do for the money
# and the messages: the signal sees the status change, raises the Payscrow
# disputes for every paid member and sends the notifications.
# ---------------------------------------------------------------------------
@login_required(login_url="signin")
@require_POST
def cancel_pool_ajax_admin(request, pk):
    if not is_copay_admin(request.user):
        return _forbidden()
 
    pool = get_object_or_404(Pool, pk=pk)
 
    if pool.status == Pool.Status.CANCELLED:
        return JsonResponse({"success": False, "error": "Pool is already cancelled."}, status=400)
 
    if pool.status == Pool.Status.COMPLETED:
        return JsonResponse({"success": False, "error": "A completed pool cannot be cancelled."}, status=400)
 
    print("========== ADMIN CANCEL POOL ==========")
    print("admin:", request.user.username, "| pool:", pool.pk)
 
    now = timezone.now()
 
    with transaction.atomic():
        # Mark every active member as having left
        for member in PoolMember.objects.filter(pool=pool, active=True):
            member.left_at = now
            member.active = False
            member.confirmed_access = False
            member.confirmed_at = None
            member.save(update_fields=["left_at", "active", "confirmed_access", "confirmed_at"])
 
        # This save is what triggers the signal (disputes + notifications)
        pool.status = Pool.Status.CANCELLED
        pool.end_date = now
        pool.active = False
        pool.save(update_fields=["status", "end_date", "active"])
 
    print(f"[CANCEL] pool #{pool.pk} cancelled. The signal now raises the disputes and sends notifications.")
 
    return JsonResponse(
        {
            "success": True,
            "message": (
                "Pool cancelled. Payscrow disputes are being created for the paid "
                "members and everyone has been notified. Check the terminal for the results."
            ),
        }
    )
 
# ---------------------------------------------------------------------------
# DISPUTE PAGE
# ---------------------------------------------------------------------------
@login_required(login_url="signin")
def dispute(request, pk):
    dispute = get_object_or_404(Dispute, pk=pk)
    pool = dispute.pool
 
    is_admin = is_copay_admin(request.user)
 
    is_involved = (
        request.user == dispute.opened_by
        or request.user == dispute.against_user
        or request.user == pool.host
        or is_admin
    )
 
    if not is_involved:
        raise Http404("You do not have permission to view this dispute.")
 
    user_membership = PoolMember.objects.filter(
        pool=pool, user=request.user, active=True
    ).first()
 
    # Admins can remove the active contributors who are part of this dispute
    removable_members = []
    if is_admin:
        party_ids = {dispute.opened_by_id, dispute.against_user_id} - {pool.host_id}
        removable_members = list(
            PoolMember.objects
            .filter(pool=pool, active=True, user_id__in=party_ids)
            .select_related("user")
        )
 
    context = {
        "dispute": dispute,
        "pool": pool,
        "user_membership": user_membership,
        "is_admin": is_admin,
        "removable_members": removable_members,
    }
 
    return render(request, "dispute.html", context)
 