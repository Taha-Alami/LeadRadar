"""
Digest composer — Stage 5 of the LeadRadar pipeline.

No LLM call is involved here. By the time this module runs, every opportunity
is already a fully structured ``EnrichedOpportunity`` — for 3-5 cards, a
deterministic Python template guarantees a consistent, correct layout every
time, with zero risk of an LLM producing broken HTML. Table-based layout for
email-client compatibility, with phase-coded badges to make actionability
scannable at a glance.

Every text field is HTML-escaped before embedding: the content originates from
scraped web pages and LLM output, neither of which should be trusted as markup.
"""
from __future__ import annotations

import html as html_lib
from datetime import datetime

from core.business_profile import get_profile
from core.news_models import EnrichedOpportunity

from pipeline.country_config import country_display_name


def _e(value: object) -> str:
    """HTML-escape any value (``None`` → empty string)."""
    return html_lib.escape(str(value or ""), quote=True)


def compose_digest_email(
    country_code: str,
    opportunities: list[EnrichedOpportunity],
    n_scanned: int,
    app_base_url: str = "http://localhost:8000",
) -> tuple[str, str]:
    """
    Build the daily lead-digest HTML email for one country.

    Args:
        country_code:  Country code (e.g. ``"SPAIN"``).
        opportunities: Best-first ``EnrichedOpportunity`` list to feature.
                       Caller is expected to have already filtered out
                       non-actionable phases and skip the call entirely when empty.
        n_scanned:     Total headlines scanned today, shown in the stats bar.
        app_base_url:  Web app URL for the "Open web app" links.

    Returns:
        ``(html, subject)`` tuple ready for ``send_email()``.
    """
    country = country_display_name(country_code)
    now     = datetime.now()
    today   = now.strftime(f"%A, %B {now.day}, %Y")

    cards   = "\n".join(_render_card(opp) for opp in opportunities)
    app_url = f"{app_base_url.rstrip('/')}/?country={country_code}"
    html    = _build_email_envelope(country, today, cards, len(opportunities), n_scanned, app_url)
    n       = len(opportunities)
    subject = f"{country} Lead Digest — {n} new lead{'s' if n != 1 else ''} — {today}"
    return html, subject


def compose_share_email(
    opp: EnrichedOpportunity,
    sender_message: str,
    enrichment_id: int | None = None,
    app_base_url: str = "http://localhost:8000",
) -> tuple[str, str]:
    """
    Build a one-off "shared opportunity" email — reuses ``_render_card()`` so
    a manually-shared lead looks exactly like one from the daily digest,
    wrapped in a lighter envelope with the sender's personal note on top
    instead of the stats bar. Used by the web app's "Send via Email" action.
    """
    card    = _render_card(opp)
    subject = f"Shared Opportunity — {opp.title}"

    message_block = ""
    if sender_message.strip():
        safe_message = _e(sender_message.strip()).replace("\n", "<br>")
        message_block = f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 18px 0;">
      <tr>
        <td style="background:#EFF6FF;border-left:3px solid #2563EB;border-radius:5px;padding:14px 18px;">
          <div style="font-size:9px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;
                       color:#2563EB;margin-bottom:6px;">Message</div>
          <div style="font-size:13px;color:#1e3a5f;line-height:1.6;">{safe_message}</div>
        </td>
      </tr>
    </table>"""

    view_button = ""
    if enrichment_id:
        view_url = _e(f"{app_base_url.rstrip('/')}/group/{int(enrichment_id)}")
        view_button = (
            f'<a href="{view_url}" style="display:inline-block;background:#1565C0;color:#ffffff;font-size:13px;'
            'font-weight:700;padding:12px 26px;border-radius:7px;text-decoration:none;letter-spacing:.3px;'
            'margin-bottom:16px;">View in app &rarr;</a><br>'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>{_e(subject)}</title>
</head>
<body style="margin:0;padding:0;background:#E8EDF5;font-family:Arial,Helvetica,sans-serif;">

<table width="100%" cellpadding="0" cellspacing="0" style="background:#E8EDF5;">
<tr><td align="center" style="padding:36px 16px 44px;">
<table width="680" cellpadding="0" cellspacing="0"
       style="background:#F3F6FB;border-radius:12px;overflow:hidden;box-shadow:0 6px 28px rgba(13,31,69,.15);">

  <!-- HEADER -->
  <tr>
    <td bgcolor="#0d1f45" style="background:linear-gradient(150deg,#060f22 0%,#0d1f45 55%,#163a6e 100%);padding:28px 36px 24px;">
      <div style="font-size:9px;color:#3a6a99;letter-spacing:3.5px;text-transform:uppercase;margin-bottom:8px;">LeadRadar</div>
      <div style="font-size:22px;font-weight:900;color:#ffffff;letter-spacing:-0.3px;">Shared Opportunity</div>
    </td>
  </tr>

  <!-- ACCENT LINE -->
  <tr><td style="height:4px;background:linear-gradient(90deg,#1a5276 0%,#2980b9 65%,#6aafd4 100%);"></td></tr>

  <!-- MESSAGE + CARD -->
  <tr>
    <td style="padding:26px 28px 12px;">
      {message_block}
      {card}
    </td>
  </tr>

  <!-- VIEW IN APP + FOOTER -->
  <tr>
    <td bgcolor="#0d1f45" style="background:#0d1f45;padding:24px 36px;">
      {view_button}
      <div style="font-size:10px;color:#3a6a99;line-height:1.9;">
        <em style="color:#2a4a68;">Shared via LeadRadar. Verify details before commercial outreach.</em>
      </div>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body>
</html>"""
    return html, subject


def _chip(label: str, value: str, bg: str, accent: str, text_color: str, pad_right: bool) -> str:
    pr = "padding-right:8px;" if pad_right else ""
    return (
        f'<td valign="top" style="{pr}">'
        f'<div style="background:{bg};border-radius:4px;padding:9px 11px;border-left:3px solid {accent};">'
        f'<div style="font-size:9px;font-weight:700;letter-spacing:1px;text-transform:uppercase;'
        f'color:{accent};margin-bottom:4px;">{_e(label)}:</div>'
        f'<div style="font-size:12px;font-weight:700;color:{text_color};line-height:1.35;">{_e(value)}</div>'
        f'</div></td>'
    )


def _render_card(opp: EnrichedOpportunity) -> str:
    fit_label = get_profile().fit_label

    # Key-facts chips — only render chips that have a value
    chip_defs = [
        (opp.company,       'Company',      '#f1f5f9', '#475569', '#0f172a'),
        (opp.project_value, 'Budget',       '#eff6ff', '#2563eb', '#1e3a8a'),
        (opp.project_type,  'Project type', '#f0fdf4', '#16a34a', '#14532d'),
        (opp.sector,        'Sector',       '#f0f9ff', '#0369a1', '#0c4a6e'),
        (opp.end_usage,     'End usage',    '#faf5ff', '#7c3aed', '#4c1d95'),
    ]
    filled = [(val, label, bg, accent, tc) for val, label, bg, accent, tc in chip_defs if val]

    if filled:
        cells = "".join(
            _chip(label, val, bg, accent, tc, pad_right=(i < len(filled) - 1))
            for i, (val, label, bg, accent, tc) in enumerate(filled)
        )
        key_facts = (
            '<table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:15px;">'
            f'<tr>{cells}</tr></table>'
        )
    else:
        key_facts = '<div style="font-size:11px;color:#9aa3b8;margin-bottom:13px;">Details pending</div>'

    url = _e(opp.url)
    return f"""
  <table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 14px 0;">
    <tr>
      <td style="background:#ffffff;border-top:3px solid {opp.phase_color};border-radius:5px;
                 padding:18px 22px;box-shadow:0 1px 5px rgba(13,31,69,.08);">

        <!-- Phase badge -->
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:10px;">
          <tr>
            <td>
              <span style="display:inline-block;background:{opp.phase_color};color:#ffffff;
                           font-size:9px;font-weight:700;letter-spacing:2px;text-transform:uppercase;
                           padding:3px 9px;border-radius:3px;">{_e(opp.phase)}</span>
            </td>
          </tr>
        </table>

        <!-- Title -->
        <div style="font-size:17px;font-weight:700;line-height:1.3;margin-bottom:12px;">
          <a href="{url}" style="color:#0d1f45;text-decoration:none;">{_e(opp.title)}</a>
        </div>

        <!-- Key facts chips: company / budget / project type / sector / end usage -->
        {key_facts}

        <!-- Why it matters -->
        <div style="font-size:13px;color:#2c3a5a;line-height:1.7;margin-bottom:14px;">
          <span style="font-size:9px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;
                       color:#64748b;">&#128269;&nbsp;Why it matters&nbsp;&nbsp;</span><br>
          {_e(opp.why_it_matters)}
        </div>

        <!-- Product fit -->
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:8px;">
          <tr>
            <td style="border-left:3px solid #C8900A;background:#FDFAF0;padding:8px 0 8px 12px;">
              <div style="font-size:9px;font-weight:700;letter-spacing:1.8px;text-transform:uppercase;
                           color:#9A6800;margin-bottom:3px;">&#9881;&nbsp;{_e(fit_label)}</div>
              <div style="font-size:12px;font-weight:600;color:#3a2a05;line-height:1.55;">{_e(opp.product_fit)}</div>
            </td>
          </tr>
        </table>

        <!-- Suggested Action -->
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:13px;">
          <tr>
            <td style="border-left:3px solid #2E7D32;background:#F2FAF2;padding:8px 0 8px 12px;">
              <div style="font-size:9px;font-weight:700;letter-spacing:1.8px;text-transform:uppercase;
                           color:#1B5E20;margin-bottom:3px;">&#9654;&nbsp;Suggested Action</div>
              <div style="font-size:12px;font-weight:600;color:#0a2e0d;line-height:1.55;">{_e(opp.recommended_action)}</div>
            </td>
          </tr>
        </table>

        <!-- Footer -->
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="border-top:1px solid #edf0f7;padding-top:9px;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td style="font-size:10px;color:#b0b8cc;">{_e(opp.source)} &middot; {_e(opp.published_date or "date unknown")}</td>
                  <td align="right">
                    <a href="{url}" style="font-size:10px;font-weight:700;letter-spacing:0.8px;
                                           color:{opp.phase_color};text-decoration:none;">READ &rsaquo;</a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>

      </td>
    </tr>
  </table>"""


def _build_email_envelope(country: str, today: str, cards_html: str, n_opportunities: int, n_scanned: int, app_url: str) -> str:
    app_url = _e(app_url)
    company = _e(get_profile().company_name)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>{_e(country)} Lead Digest - {today}</title>
</head>
<body style="margin:0;padding:0;background:#E8EDF5;font-family:Arial,Helvetica,sans-serif;">

<table width="100%" cellpadding="0" cellspacing="0" style="background:#E8EDF5;">
<tr><td align="center" style="padding:28px 12px 36px;">
<table width="620" cellpadding="0" cellspacing="0"
       style="background:#F3F6FB;border-radius:8px;overflow:hidden;box-shadow:0 4px 24px rgba(13,31,69,.15);">

  <!-- HEADER -->
  <tr>
    <td bgcolor="#0d1f45" style="background:linear-gradient(150deg,#060f22 0%,#0d1f45 55%,#163a6e 100%);padding:26px 30px 22px;">
      <div style="font-size:9px;color:#3a6a99;letter-spacing:3.5px;text-transform:uppercase;margin-bottom:10px;">LeadRadar &middot; {company}</div>
      <div style="font-size:22px;font-weight:900;color:#ffffff;letter-spacing:-0.3px;line-height:1.2;text-transform:uppercase;">{_e(country)} Lead Digest</div>
      <div style="font-size:11px;color:#5a8ab0;margin-top:6px;letter-spacing:0.3px;">{today}</div>
    </td>
  </tr>

  <!-- ACCENT LINE -->
  <tr><td style="height:3px;background:linear-gradient(90deg,#1a5276 0%,#2980b9 65%,#6aafd4 100%);"></td></tr>

  <!-- STATS BAR -->
  <tr>
    <td bgcolor="#1a2f5e" style="background:#1a2f5e;padding:10px 30px;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="font-size:11px;color:#4a7aaa;">
            <strong style="color:#ffffff;font-size:15px;">{n_opportunities}</strong>
            &nbsp;top lead{'s' if n_opportunities != 1 else ''}
            &nbsp;&nbsp;&middot;&nbsp;&nbsp;
            <strong style="color:#ffffff;font-size:15px;">{n_scanned}</strong>
            &nbsp;headlines scanned
          </td>
          <td align="right">
            <a href="{app_url}"
               style="display:inline-block;font-size:10px;font-weight:700;color:#ffffff;
                      background:#1a5276;padding:5px 13px;border-radius:4px;text-decoration:none;
                      letter-spacing:0.5px;border:1px solid #2980b9;">
              Open Web App &rsaquo;
            </a>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- CARDS -->
  <tr>
    <td style="padding:20px 22px 8px;">
      {cards_html}
    </td>
  </tr>

  <!-- FOOTER -->
  <tr>
    <td bgcolor="#0d1f45" style="background:#0d1f45;padding:16px 30px;">
      <div style="font-size:10px;color:#3a6a99;line-height:1.9;">
        <strong style="color:#6a9abf;">{_e(country)} Lead Digest</strong> &middot; LeadRadar &middot; {today}<br>
        <em style="color:#2a4a68;">AI-assisted lead generation. Verify details before commercial outreach.</em><br>
        <a href="{app_url}"
           style="color:#4a8ab8;text-decoration:none;">
          &#8594; Go to Web App
        </a>
      </div>
    </td>
  </tr>

</table>
</td></tr>
</table>
</body>
</html>"""
