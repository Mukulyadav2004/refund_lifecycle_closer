"""Refund Lifecycle Closer — Razorpay AI Buildathon, AI Finance Controller track.

Closes the refund lifecycle across a month of merchant activity by joining four
Razorpay endpoints and one merchant-side ledger, resolving every refund to
exactly one closure state, and reporting measured accuracy against seeded
ground truth.

Claim discipline: three legs verified (initiated, gateway-processed, deducted
from settlement) and one leg evidenced (ARN/RRN). Never "four legs".
"""

__version__ = "0.1.0"
