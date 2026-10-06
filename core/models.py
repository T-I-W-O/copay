from decimal import Decimal
from django.utils import timezone
from django.contrib.auth.models import User
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.core.validators import MinValueValidator, MaxValueValidator
from decimal import Decimal
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.db import models
from decimal import Decimal
from django.db import models
from django.contrib.auth import models as auth_models # assuming standard User model


class UserProfile(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="profile"
    )

    phone_number = models.CharField(max_length=20)
    whatsapp_number = models.CharField(max_length=20, blank=True)

    bank = models.CharField(max_length=100, blank=True)
    bank_code = models.CharField(max_length=150, blank=True)
    account_number = models.CharField(max_length=255, blank=True)
    account_name = models.CharField(max_length=150, blank=True)
    bank_verified = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.user.username


class Service(models.Model):

    class Category(models.TextChoices):
        VIDEO = "video", "Video"
        MUSIC = "music", "Music"
        AI_TOOL = "aitool", "AI Tool"
        SOFTWARE = "software", "Software"
        GAMING = "gaming", "Gaming"
        OTHER = "other", "Other"

    class Type(models.TextChoices):
        STANDARD = "standard", "Standard"
        MANMADE = "manmade", "Manmade"

    name = models.CharField(max_length=100, unique=True)

    category = models.CharField(
        max_length=20,
        choices=Category.choices,
        default=Category.OTHER
    )

    type = models.CharField(
        max_length=20,
        choices=Type.choices,
        default=Type.STANDARD
    )

    standard_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))]
    )

    max_people = models.PositiveIntegerField(
        validators=[MinValueValidator(1)]
    )

    image = models.ImageField(
        upload_to="services/",
        blank=True,
        null=True
    )

    def __str__(self):
        return self.name

class Pool(models.Model):

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSE = "close", "Close"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    host = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="hosted_pools"
    )

    service = models.ForeignKey(
        Service,
        on_delete=models.PROTECT,
        related_name="pools"
    )

    # Price set by the host for this particular pool
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))]
    )

    # Number of people the host wants in this pool.
    # Must never exceed Service.max_people.
    number_of_users = models.PositiveIntegerField(
        validators=[MinValueValidator(1)]
    )

    duration_days = models.PositiveIntegerField(
        validators=[MinValueValidator(1)]
    )
    end_date = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN
    )

    escrow_reference = models.CharField(
        max_length=255,
        blank=True
    )

    created_at = models.DateTimeField(default=timezone.now)
    is_public = models.BooleanField(default=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.service.name} - {self.host.username}"

    
    @property
    def amount_per_person(self):
        if self.number_of_users == 0:
            return Decimal("0.00")
        return self.price / self.number_of_users

    @property
    def current_users(self):
        # Requirement 1: Only count members that are active
        return self.members.filter(active=True).count()
    

class PoolMember(models.Model):

    class Role(models.TextChoices):
        CONTRIBUTOR = "contributor", "Contributor"

    class PaymentStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        REFUNDED = "refunded", "Refunded"

    pool = models.ForeignKey(
        Pool,
        on_delete=models.CASCADE,
        related_name="members"
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="pool_memberships"
    )

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.CONTRIBUTOR
    )

    amount_paid = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00")
    )

    payment_status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING
    )

    confirmed_access = models.BooleanField(default=False)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)
    dispute = models.BooleanField(default=False)
    # In models.py (PoolMember model)
    joined_at = models.DateTimeField(null=True, blank=True)
    left_at = models.DateTimeField(null=True, blank=True)



    def __str__(self):
        return f"{self.user.username} - {self.pool}"
    
#--------------------------------------------------------------------

class Payment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"  # <--- Added this back
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"

    pool_member = models.ForeignKey(
        "PoolMember", on_delete=models.CASCADE, related_name="payments", null=True
    )
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="payments"
    )
    stint_started_at = models.DateTimeField(null=True, blank=True)  # NEW
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    copay_fees = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    provider = models.CharField(max_length=50, default="payscrow")
    reference = models.CharField(max_length=255, unique=True)
    ecrow_code =  models.CharField(max_length=255, null=True, blank=True)
    transaction_guid = models.CharField(max_length=255, unique=True, null=True, blank=True)
    
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PAID
    )
    
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    payscrow_dispute_raised = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.reference} - {self.status}"

 

class Dispute(models.Model):

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"

    pool = models.ForeignKey(
        Pool,
        on_delete=models.PROTECT,
        related_name="disputes"
    )
    name = models.CharField(max_length=100, unique=True, null=True, blank=True)

    opened_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="opened_disputes"
    )

    against_user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="disputes_against"
    )

    reason = models.TextField()

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN
    )

    resolution = models.TextField(blank=True)

    resolved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_disputes"
    )

    created_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Dispute #{self.id} - {self.pool}"
   
class SiteInfo(models.Model):
    email = models.EmailField()
    phone = models.CharField(max_length=20, blank=True)
    site_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal("0.00")),
            MaxValueValidator(Decimal("100.00")),
        ],
        null=True,
        blank=True
    )

    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal("0.00")),
        ],
        null=True,
        blank=True
    )
    bank = models.CharField(max_length=100, blank=True)
    bank_code = models.CharField(max_length=150, blank=True)
    account_number = models.CharField(max_length=255, blank=True)
    account_name = models.CharField(max_length=150, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.email

class Refund(models.Model):
    pool_member = models.ForeignKey(PoolMember, on_delete=models.CASCADE, related_name="refunds")  # was OneToOne
    payment = models.OneToOneField(Payment, on_delete=models.CASCADE, related_name="refund", null=True, blank=True)        # NEW — replaces the uniqueness Refund used to get from pool_member
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=20,
        choices=[
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("completed", "Completed"),
            ("failed", "Failed"),
        ],
        default="pending",
    )
    payment_reference = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        unique=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"Refund - {self.pool_member.user.username} - ₦{self.amount}"


class PaymentHistory(models.Model):
    class Kind(models.TextChoices):
        EARNING = "earning", "Earning (Host Payout)"
        COMMISSION = "commission", "Platform Commission"
        REFUND = "refund", "Refund"

    user = models.ForeignKey(
        auth_models.User,
        on_delete=models.CASCADE,
        related_name="payment_history",
    )
    
    # Optional: Keep a link to the pool for easy grouping on pool pages
    pool = models.ForeignKey(
        "Pool",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payment_history",
    )
    
    # Optional link back to your initial Payment table for complete audit trails
    payment = models.ForeignKey(
        "Payment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="history_records"
    )

    kind = models.CharField(
        max_length=20,
        choices=Kind.choices,
    )
    
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    
    payment_reference = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.get_kind_display()} - {self.user.username} - ₦{self.amount}"
    
    
class Notification(models.Model):

    class Type(models.TextChoices):
        PAYMENT = "payment", "Payment"
        POOL = "pool", "Pool"
        SECURITY = "security", "Security"
        DISPUTE = "dispute", "Dispute"
        RATING = "rating", "Rating"
        REMINDER = "reminder", "Reminder"
        SYSTEM = "system", "System"

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="notifications",
    )

    pool = models.ForeignKey(
        Pool,
        on_delete=models.CASCADE,
        related_name="notifications",
        null=True,
        blank=True,
    )

    type = models.CharField(
        max_length=20,
        choices=Type.choices,
    )

    title = models.CharField(max_length=150)

    description = models.TextField()

    url = models.CharField(
        max_length=255,
        blank=True,
        null=True,
    )

    is_read = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.title}"
  
#------------------------------------------------------------------------------
    
class PoolVault(models.Model):
    pool = models.OneToOneField(
        Pool,
        on_delete=models.CASCADE,
        related_name="vault"
    )

    login_method = models.CharField(max_length=50)

    credentials = models.JSONField(default=dict)

    instructions = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
class PasswordResetOTP(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="password_reset_otps"
    )

    otp = models.CharField(max_length=6)

    expires_at = models.DateTimeField()

    is_used = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Password reset - {self.user.username}"
  
class Rating(models.Model):
    pool = models.ForeignKey(
        "Pool",
        on_delete=models.CASCADE,
        related_name="ratings"
    )

    reviewer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="given_ratings"
    )

    host = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="received_ratings"
    )

    rating = models.PositiveSmallIntegerField(
        validators=[
            MinValueValidator(1),
            MaxValueValidator(5)
        ]
    )

    created_at = models.DateTimeField(auto_now_add=True)
    
class Reputation(models.Model):
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="reputation"
    )

    successful_pools = models.PositiveIntegerField(default=0)

    resolved_disputes = models.PositiveIntegerField(default=0)

    unresolved_disputes = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} reputation"
   