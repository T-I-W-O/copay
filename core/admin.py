from django.contrib import admin
from .models import *


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "phone_number",
        "whatsapp_number",
        "bank",
        "account_name",
        "bank_verified",
        "created_at",
    )
    search_fields = (
        "user__username",
        "user__email",
        "phone_number",
        "whatsapp_number",
        "account_name",
    )
    list_filter = ("bank_verified",)


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "max_people",
        "standard_price",
        "category",
        "image"
    )
    search_fields = ("name",)


@admin.register(Pool)
class PoolAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "host",
        "service",
        "price",
        "number_of_users",
        "status",
        "end_date",
        "created_at",
      
    )
    search_fields = (
        "host__username",
        "host__email",
        "service__name",
        "escrow_reference",
    )
    list_filter = (
        "status",
        "service",
    )



@admin.register(PoolMember)
class PoolMemberAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "pool",
        "role",
        "active",
        "amount_paid",
        "payment_status",
        "confirmed_access",
        "joined_at",
    )
    search_fields = (
        "user__username",
        "user__email",
        "pool__service__name",
    )
    list_filter = (
        "role",
        "payment_status",
        "confirmed_access",
    )


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "user",
        "pool_member",
        "amount",
        "provider",
        "status",
        "paid_at",
        "created_at",
    )
    list_filter = ("status", "provider", "created_at")
    search_fields = (
        "reference",
        "user__username",
        "user__email",
    )
    readonly_fields = ("created_at",)


@admin.register(Reputation)
class ReputationAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "resolved_disputes",
        "unresolved_disputes",
        "successful_pools",
    )
    search_fields = (
        "user__username",
        "user__email",
    )
    readonly_fields = (
        "created_at",
    )


@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "pool",
        "opened_by",
        "against_user",
        "status",
        "created_at",
        "resolved_at",
    )
    search_fields = (
        "opened_by__username",
        "against_user__username",
        "pool__service__name",
        "reason",
    )
    list_filter = (
        "status",
    )

@admin.register(SiteInfo)
class SiteInfoAdmin(admin.ModelAdmin):
    list_display = (
        "email",
        "phone",
        "updated_at",
        "site_percentage",
    )
    readonly_fields = (
        "updated_at",
    )
    
@admin.register(PoolVault)
class PoolVaultAdmin(admin.ModelAdmin):
    list_display = (
        "login_method",
        "credentials",
        "instructions",
    )
    readonly_fields = (
        "updated_at",
    )
    

    
@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "pool",
        "type",
        "title",
        "is_read",
        "created_at",
    )

    list_filter = (
        "type",
        "is_read",
        "created_at",
    )

    search_fields = (
        "user__username",
        "title",
        "description",
        "pool__service__name",
    )

    readonly_fields = (
        "created_at",
    )

    ordering = (
        "-created_at",
    )