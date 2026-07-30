#!/usr/bin/env python3
"""
morning_report.py — Build the overnight stock-research report and email it.

Mirrors the old morning-review bash script:
  * finds the newest findings/report_*.md
  * finds the newest findings/results_*.json and summarises it
  * converts the combined Markdown to HTML with pypandoc
  * emails a mobile-friendly HTML email (with a plain-text fallback)

Configuration is read from environment variables. A .env file in the project
directory is loaded automatically via config.py.

  FINDINGS_DIR    directory holding report_*.md / results_*.json
                  (default: ~/stock-explorer-agent/findings)
  SMTP_HOST       SMTP server hostname              (required to send)
  SMTP_PORT       SMTP server port                  (default: 587)
  SMTP_USER       SMTP username                     (optional)
  SMTP_PASSWORD   SMTP password / app password      (optional)
  SMTP_SECURITY   "starttls" | "ssl" | "none"       (default: starttls)
  SMTP_HELO       HELO/EHLO FQDN to present         (default: system hostname)
  MAIL_FROM       From: address (USE A REAL DOMAIN) (default: SMTP_USER)
  MAIL_REPLY_TO   Reply-To: address                 (optional)
  MAIL_TO         comma-separated recipient list    (or pass --to)

Deliverability (optional DKIM signing — see notes at bottom of file):
  DKIM_DOMAIN       signing domain, e.g. sumo.computer
  DKIM_SELECTOR     selector, e.g. mail   (DNS: <selector>._domainkey.<domain>)
  DKIM_PRIVATE_KEY  path to the PEM private key (or the PEM text itself)

Usage:
  python morning_report.py                       # find, build, email
  python morning_report.py --to a@x.com,b@y.com  # override recipient list
  python morning_report.py --no-email            # just print to stdout
  python morning_report.py --out report.html     # also save the HTML

Dependencies:
  pip install pypandoc-binary
  pip install dkimpy            # only if you enable DKIM signing
"""

from __future__ import annotations

import argparse
import json
import logging
import smtplib
import sys
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

try:
    import config  # noqa: F401 -- importing config loads .env into os.environ
except ImportError:
    pass

import os

log = logging.getLogger("morning_report")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class MailConfig:
    host: str | None
    port: int
    user: str | None
    password: str | None
    security: str
    mail_from: str
    reply_to: str | None
    helo: str | None
    recipients: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, recipients_override: list[str] | None = None) -> "MailConfig":
        user = os.environ.get("SMTP_USER")
        recipients = recipients_override or _split_csv(os.environ.get("MAIL_TO", ""))
        return cls(
            host=os.environ.get("SMTP_HOST"),
            port=int(os.environ.get("SMTP_PORT", "587")),
            user=user,
            password=os.environ.get("SMTP_PASSWORD"),
            security=os.environ.get("SMTP_SECURITY", "starttls").lower(),
            mail_from=os.environ.get("MAIL_FROM") or user or "stock-agent@localhost",
            reply_to=os.environ.get("MAIL_REPLY_TO"),
            helo=os.environ.get("SMTP_HELO"),
            recipients=recipients,
        )

    def validate(self) -> None:
        if not self.host:
            raise SystemExit("SMTP_HOST is not set — cannot send email.")
        if not self.recipients:
            raise SystemExit("No recipients — set MAIL_TO or pass --to.")
        if self.security not in {"starttls", "ssl", "none"}:
            raise SystemExit(f"Invalid SMTP_SECURITY: {self.security!r}")
        if self.mail_from.endswith("@localhost"):
            # Not fatal, but this is the #1 cause of Outlook junking.
            log.warning(
                "MAIL_FROM=%s uses a non-routable domain; this will fail SPF/DKIM "
                "alignment and is likely to be junked. Set MAIL_FROM to a real "
                "address on a domain you control.",
                self.mail_from,
            )


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def findings_dir() -> Path:
    return Path(
        os.environ.get("FINDINGS_DIR", "~/stock-explorer-agent/findings")
    ).expanduser()


# --------------------------------------------------------------------------- #
# Report building
# --------------------------------------------------------------------------- #
def find_latest(directory: Path, pattern: str) -> Path | None:
    matches = sorted(
        directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return matches[0] if matches else None


def summarise_results(json_path: Path) -> str:
    """Return a Markdown 'Quick Stats' section, or a note if it can't be read."""
    try:
        data = json.loads(json_path.read_text())
    except Exception as exc:  # malformed JSON, permissions, etc.
        log.warning("Could not read stats from %s: %s", json_path, exc)
        return f"_Could not read quick stats from `{json_path.name}`: {exc}_\n"

    lines = [
        "## 📈 Quick Stats",
        "",
        f"- **Strategies executed:** {data.get('total_strategies_run', '?')}",
        f"- **Unique tickers found:** {data.get('total_tickers_found', '?')}",
    ]
    multi = data.get("multi_signal_stocks", {}) or {}
    lines.append(f"- **Multi-signal stocks:** {len(multi)}")

    if multi:
        lines += ["", "**🔥 Top conviction picks:**", ""]
        top = sorted(multi.items(), key=lambda kv: len(kv[1]), reverse=True)[:5]
        for ticker, signals in top:
            joined = ", ".join(signals)
            lines.append(f"- **{ticker}** ({len(signals)} signals: {joined})")

    return "\n".join(lines) + "\n"


def build_markdown(directory: Path) -> tuple[str, str]:
    """Return (subject, markdown_body) for the morning report."""
    today = datetime.now().strftime("%A, %B %d, %Y")
    report = find_latest(directory, "report_*.md")
    results = find_latest(directory, "results_*.json")

    parts = [
        "# 🌅 Good Morning — Overnight Stock Research Report",
        "",
        f"_{today}_",
        "",
    ]

    if report:
        log.info("Latest report: %s", report.name)
        parts += [f"**Latest report:** `{report.name}`", "", report.read_text()]
    else:
        log.info("No report files found in %s", directory)
        parts.append("⏳ No reports generated yet. The first run is scheduled for 2 AM.")

    if results:
        log.info("Latest data: %s", results.name)
        parts += ["", "---", "", summarise_results(results)]

    subject = f"Stock Research Report — {today}"
    return subject, "\n".join(parts)


# --------------------------------------------------------------------------- #
# HTML conversion — responsive, email-client-safe (incl. Outlook/Word engine)
# --------------------------------------------------------------------------- #
# Notes on the approach (why not Bootstrap):
#   * Email clients strip <link> stylesheets, so external CSS never loads.
#   * Outlook desktop uses Word to render: no flexbox/grid, no media queries.
#   * So: a centered table (Outlook-safe), an inline <style> block for the
#     things Outlook DOES honor (basic typography/borders/colors via classes),
#     plus a max-width:600px media query for true mobile clients (iOS/Apple
#     Mail/Gmail). The layout looks correct even when the media query is ignored.
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>{title}</title>
<!--[if mso]>
<noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript>
<style>table,td,div,p,h1,h2,h3,a {{font-family:Arial,Helvetica,sans-serif !important;}}</style>
<![endif]-->
<style>
  body,table,td {{ margin:0; padding:0; }}
  body {{ width:100% !important; background:#f4f5f7; -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%; }}
  img {{ border:0; line-height:100%; outline:none; text-decoration:none; -ms-interpolation-mode:bicubic; }}
  .wrapper {{ width:100%; background:#f4f5f7; }}
  .container {{ width:600px; max-width:600px; margin:0 auto; background:#ffffff; }}
  .content {{ padding:28px; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; color:#1a1a1a; font-size:16px; line-height:1.55; }}
  .content h1 {{ font-size:22px; line-height:1.3; margin:0 0 8px; color:#0b1f33; }}
  .content h2 {{ font-size:18px; line-height:1.3; margin:28px 0 8px; color:#0b1f33; }}
  .content h3 {{ font-size:16px; margin:20px 0 6px; color:#0b1f33; }}
  .content p, .content li {{ font-size:16px; margin:0 0 12px; }}
  .content a {{ color:#1b6ec2; }}
  .content code {{ background:#f2f2f2; padding:1px 5px; border-radius:4px; font-size:14px; font-family:Consolas,Menlo,Monaco,monospace; }}
  .content pre {{ background:#f6f8fa; padding:12px; border-radius:6px; overflow-x:auto; font-size:13px; }}
  .content table {{ border-collapse:collapse; width:100%; margin:16px 0; }}
  .content th, .content td {{ border:1px solid #dddddd; padding:8px 10px; text-align:left; font-size:14px; vertical-align:top; }}
  .content th {{ background:#f6f8fa; }}
  .content hr {{ border:none; border-top:1px solid #e0e0e0; margin:24px 0; }}
  .content blockquote {{ border-left:3px solid #cccccc; margin:16px 0; padding-left:12px; color:#555555; }}
  .footer {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif; font-size:12px; line-height:1.5; color:#8a8a8a; padding:16px 28px 28px; text-align:center; }}
  @media only screen and (max-width:600px) {{
    .container {{ width:100% !important; max-width:100% !important; }}
    .content {{ padding:18px !important; font-size:17px !important; }}
    .content p, .content li {{ font-size:17px !important; }}
    .content h1 {{ font-size:20px !important; }}
    .content h2 {{ font-size:17px !important; }}
    /* let any wide tables scroll instead of overflowing the screen */
    .content table {{ display:block; overflow-x:auto; -webkit-overflow-scrolling:touch; white-space:nowrap; }}
  }}
</style>
</head>
<body>
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;font-size:1px;line-height:1px;color:#f4f5f7;">{title} — overnight signals and conviction picks.</div>
<table role="presentation" class="wrapper" width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr>
    <td align="center" style="padding:16px;">
      <!--[if mso]><table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->
      <table role="presentation" class="container" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:600px;background:#ffffff;">
        <tr><td class="content">
{body}
        </td></tr>
        <tr><td class="footer">
          Generated by Stock Explorer Agent on MS-A1.<br>
          Automated research summary — informational only, not investment advice.
        </td></tr>
      </table>
      <!--[if mso]></td></tr></table><![endif]-->
    </td>
  </tr>
</table>
</body>
</html>"""


def to_html(markdown_text: str, title: str) -> str:
    try:
        import pypandoc
    except ImportError:
        raise SystemExit("pypandoc is required: pip install pypandoc-binary")

    try:
        body = pypandoc.convert_text(markdown_text, "html", format="gfm")
    except OSError:
        # No pandoc executable on PATH — fetch one (no-op with pypandoc-binary).
        log.info("pandoc not found; downloading via pypandoc...")
        pypandoc.download_pandoc()
        body = pypandoc.convert_text(markdown_text, "html", format="gfm")

    return HTML_TEMPLATE.format(title=title, body=body)


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def _maybe_dkim_sign(msg: EmailMessage) -> None:
    """If DKIM_* env vars are set, add a DKIM-Signature header in place.

    Best-effort: a signing failure is logged but does not block the send
    (an unsigned report is better than no report).
    """
    domain = os.environ.get("DKIM_DOMAIN")
    selector = os.environ.get("DKIM_SELECTOR")
    key_ref = os.environ.get("DKIM_PRIVATE_KEY")
    if not (domain and selector and key_ref):
        return

    try:
        import dkim
    except ImportError:
        log.warning("DKIM_* configured but dkimpy not installed; sending unsigned. "
                    "pip install dkimpy")
        return

    try:
        key_path = Path(key_ref).expanduser()
        privkey = key_path.read_bytes() if key_path.is_file() else key_ref.encode()
        sig = dkim.sign(
            message=msg.as_bytes(),
            selector=selector.encode(),
            domain=domain.encode(),
            privkey=privkey,
            include_headers=[b"from", b"to", b"subject", b"date",
                             b"message-id", b"mime-version", b"content-type"],
        )
        # dkim.sign returns b"DKIM-Signature: ....\r\n"; strip the field name.
        header_value = sig.decode("ascii").split(":", 1)[1].strip()
        msg["DKIM-Signature"] = header_value
        log.info("DKIM-signed as %s (selector=%s)", domain, selector)
    except Exception as exc:
        log.warning("DKIM signing failed (%s); sending unsigned.", exc)


def send_email(cfg: MailConfig, subject: str, html_body: str, text_body: str) -> None:
    cfg.validate()

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.mail_from
    msg["To"] = ", ".join(cfg.recipients)
    if cfg.reply_to:
        msg["Reply-To"] = cfg.reply_to
    # Headers that legitimate mail always has; their absence is a spam signal.
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg.mail_from.split("@")[-1] or None)
    # RFC 3834: mark machine-generated mail so clients don't auto-reply to it.
    msg["Auto-Submitted"] = "auto-generated"

    msg.set_content(text_body)  # plain-text fallback
    msg.add_alternative(html_body, subtype="html")

    _maybe_dkim_sign(msg)

    log.info("Sending to %s via %s:%s", ", ".join(cfg.recipients), cfg.host, cfg.port)

    smtp_cls = smtplib.SMTP_SSL if cfg.security == "ssl" else smtplib.SMTP
    # local_hostname controls the HELO/EHLO name — must be a FQDN, never 'localhost'.
    with smtp_cls(cfg.host, cfg.port, local_hostname=cfg.helo, timeout=30) as smtp:
        smtp.ehlo()
        if cfg.security == "starttls":
            smtp.starttls()
            smtp.ehlo()
        if cfg.user and cfg.password:
            smtp.login(cfg.user, cfg.password)
        smtp.send_message(msg)

    log.info("Email sent.")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and email the morning report.")
    parser.add_argument("--to", help="Comma-separated recipients (overrides MAIL_TO).")
    parser.add_argument("--out", help="Also write the generated HTML to this path.")
    parser.add_argument(
        "--no-email", action="store_true", help="Build only; print to stdout, don't send."
    )
    parser.add_argument(
        "--findings-dir", help="Override FINDINGS_DIR for this run."
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)

    directory = (
        Path(args.findings_dir).expanduser() if args.findings_dir else findings_dir()
    )
    if not directory.is_dir():
        log.error("Findings directory not found: %s", directory)
        return 1

    subject, markdown_body = build_markdown(directory)
    html_body = to_html(markdown_body, subject)

    if args.out:
        Path(args.out).write_text(html_body, encoding="utf-8")
        log.info("Wrote HTML to %s", args.out)

    if args.no_email:
        print(html_body)
        return 0

    recipients = _split_csv(args.to) if args.to else None
    cfg = MailConfig.from_env(recipients)
    send_email(cfg, subject, html_body, markdown_body)
    return 0


if __name__ == "__main__":
    sys.exit(main())