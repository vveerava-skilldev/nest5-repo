"""UPI QR payload + payee details for the owner "Pay now" sheet.

UPI never reports back to the portal: the QR only carries the payee, amount and note for the
payer's UPI app. A payment is recorded (and counts) the moment the owner submits the pay form; the admin
verifies the UTR against the UPI statement afterwards and can reject it, which makes the due reappear.
"""
import io
import re
from urllib.parse import quote

import qrcode
import qrcode.image.svg

from app.config import settings

NOTE_MAX = 50

# PhonePe declines prefilled-amount payment links to a personal UPI ID ("declined for security
# reason"), so the sheet only launches the app (plain scheme, no payee or amount); the owner types
# the UPI ID / mobile number shown on the sheet. (The QR endpoint remains but is no longer linked.)
PHONEPE_LAUNCH = "phonepe://"

UTR_RE = re.compile(r"^[A-Za-z0-9]{8,30}$")
UTR_HELP = "Enter the UPI reference (UTR): 8 to 30 letters or digits, no spaces."


def utr_error(payment_mode: str | None, reference: str | None) -> str | None:
    """Owners must give the UPI reference (UTR) when they say they paid by UPI."""
    if payment_mode != "UPI":
        return None
    ref = (reference or "").strip()
    if not ref:
        return "Enter the UPI reference (UTR) from your payment — it is required for UPI payments."
    return None if UTR_RE.match(ref) else UTR_HELP


def _params(amount: int | float, note: str, vpa: str | None, payee: str | None) -> str:
    params = [
        ("pa", vpa or settings.upi_vpa),
        ("pn", payee or settings.upi_payee_name),
        ("am", f"{float(amount):.2f}"),
        ("cu", "INR"),
        ("tn", note.strip()[:NOTE_MAX]),
    ]
    return "&".join(f"{k}={quote(str(v), safe='@')}" for k, v in params)


def build_link(amount: int | float, note: str, *, vpa: str | None = None, payee: str | None = None) -> str:
    return "upi://pay?" + _params(amount, note, vpa, payee)


def payee_details(vpa: str | None = None, payee: str | None = None, mobile: str | None = None) -> dict:
    """Callers pass the live Payment settings; the config values are only the fallback seed."""
    return {
        "upi_vpa": vpa or settings.upi_vpa,
        "upi_payee": payee or settings.upi_payee_name,
        "upi_mobile": mobile or settings.upi_mobile,
        "phonepe_launch": PHONEPE_LAUNCH,
    }


def qr_svg(data: str) -> bytes:
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()
