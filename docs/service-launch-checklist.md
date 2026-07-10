# Service Launch Checklist — SiteDetective Monitoring

**Status:** Living document
**Date started:** 2026-07-08
**Goal:** first paying client for the site-monitoring service built on
SiteDetective ("we watch your store's critical flows and alert you before
your customers notice").

Work through the phases in order — each phase gates the next. Items marked
**[code]** are engineering work in this repo; everything else is business
setup only you can do.

---

## Phase 0 — Product readiness (the "don't embarrass yourself" gate)

The two failure modes that kill a monitoring service on day one are alert
spam and false alarms. Don't onboard anyone — even free pilots — before
these are closed.

- [ ] **[code]** Alert triage Phase 1 — per-test health state, client email
      only on down/recovered transitions, operator webhook gets everything
      (`docs/spec-alert-triage.md` §4). *Blocking: without this, one outage
      sends the client ~4 identical emails per hour.*
- [ ] **[code]** Alert triage Phase 2 — confirmation re-run before declaring
      an outage (`spec-alert-triage.md` §5). *Blocking: kills the single
      biggest false-alarm source (transient timeouts).*
- [ ] **[code]** Healing tiers Phase 1 — deterministic intent-text healing +
      gate change (`docs/spec-healing-tiers.md` §5). *Strongly recommended:
      cosmetic theme changes stop paging you.*
- [ ] **[code]** Uptime summary per test over a period (pass rate, incidents,
      downtime minutes) — needed for the monthly client email. Builds on the
      client report work (`spec-page-diagnostics.md` §5.3) or ships as a
      minimal query + template first.
- [ ] **[code]** Custom User-Agent string for the runner browser
      (`SiteDetective-Monitor/1.0`) so client WAFs/analytics can identify and
      filter monitoring traffic.

## Phase 1 — Infrastructure (one weekend)

- [ ] Always-on host: a small VPS (~$10–20/mo) or a dedicated always-on
      machine at home. One SiteDetective instance **per client** (own
      directory, config, SQLite, port) — this is the isolation model.
- [ ] Process supervision: each instance auto-starts and auto-restarts
      (Windows: NSSM or Task Scheduler; Linux: systemd units).
- [ ] Dead-man's switch: each instance pings a free monitor
      (e.g. Healthchecks.io) on every scheduler tick — a silent crash of the
      monitoring service itself must page you. *Non-negotiable.*
- [ ] Backups: nightly copy of each client's `data/` (SQLite), `tests/`, and
      `config/` off the host.
- [ ] Email: transactional provider account (Brevo free tier to start;
      Postmark when revenue justifies it), sending domain, SPF + DKIM records
      verified. Send yourself a test alert and confirm it lands in inbox,
      not spam.
- [ ] Operator channel: Slack workspace + incoming webhook per client
      instance (one channel, or one per client), set as
      `NOTIFY_WEBHOOK_URL`. All failures flow here first.
- [ ] Dogfood: monitor 1–2 real sites you control (or any public site,
      read-only flows) for **two uninterrupted weeks**. Track every alert:
      real, flaky, or bug. Target before taking pilots: a false-alarm rate
      you'd tolerate as a paying customer.

## Phase 2 — Business shell (a few evenings, mostly waiting)

- [ ] Name + domain for the service (the sending domain from Phase 1 can be
      this domain — decide before buying).
- [ ] One-page website: what it does in one sentence, who it's for, 3-bullet
      how-it-works, pricing, contact/booking link. No blog, no login — a
      brochure.
- [ ] Pricing decision (starting point: $75–100/mo per site, optional $150
      setup fee, first month free, cancel anytime).
- [ ] Payment rail: Stripe Payment Links or invoicing — whatever is lowest
      friction in your country. Recurring billing automation can wait until
      ~5 paying clients.
- [ ] Business registration / tax treatment for your jurisdiction. Rules
      vary too much to encode here — check locally before the first paid
      invoice (pilots-for-free need nothing).
- [ ] Service agreement (one page, plain language):
  - scope: which flows, check frequency, alert channels
  - **written authorization to run automated tests against their site**
  - what monitoring is *not*: no uptime guarantee, no fix-it service,
    synthetic checks only
  - liability cap (fees paid), data handling (what you store: screenshots,
    URLs, test account credentials), termination (either party, no notice
    period needed at this price point)
  - have someone qualified sanity-check it once; reuse forever.

## Phase 3 — Sales assets (one evening)

- [ ] Client onboarding checklist template (copy per client):
  - [ ] signed agreement incl. testing authorization
  - [ ] flows agreed (default set: homepage, search, add-to-cart,
        checkout-to-payment-page, login)
  - [ ] dedicated test account + email (e.g. `monitor@<yourservice>.com`),
        excluded from their marketing automations
  - [ ] your server IP + User-Agent allowlisted in their WAF/bot protection
  - [ ] your IP/UA filtered from their analytics
  - [ ] alert recipient(s) confirmed; test alert sent and acknowledged
  - [ ] flows written, run green for 48h before "monitoring active" email
- [ ] Demo kit: one sample alert email (screenshot), one sample monthly
      report, one screenshot of a run detail with a caught failure. Real
      artifacts from your dogfood period, lightly anonymized.
- [ ] Outreach templates:
  - warm: 3 sentences — what you built, what it catches, free month offer.
  - cold ("found something" variant): run your flows against a prospect's
    site first; open with the concrete broken thing + screenshot. Only send
    cold email when you actually found something.
- [ ] Prospect list: 20–30 names. Order: people you know with stores →
      local businesses → web agencies (one agency = many sites) →
      Shopify/WooCommerce community contacts.

## Phase 4 — Pilots → paying (weeks 3–8)

- [ ] Onboard 3–5 pilots, free for 30 days, using the onboarding checklist.
      Cap it at 5 — the point is learning, not scale.
- [ ] During pilots, track per client: alerts sent (real vs. false), incidents
      caught, time you spent (setup + triage). This is your unit-economics
      data.
- [ ] Mid-pilot touchpoint (day ~14): send each pilot their stats so far —
      "your site was up 99.9%, we caught X" — even when nothing happened.
      Silence reads as worthlessness; the touchpoint is the product.
- [ ] Ask each pilot for a testimonial + permission to name them, in exchange
      for a discount on their first paid months.
- [ ] Convert: at day 25, a plain email — stats recap, price, payment link.
      Expect 2–3 of 5 to convert; below that, the pitch or the false-alarm
      rate needs fixing before widening outreach.

## Exit criteria for "launched"

- ≥ 2 paying clients
- False-alarm rate over the last 30 days you'd accept as a customer
- An outage was caught and alerted before the client noticed it themselves
  (this becomes your homepage headline)
- Total ops time < ~1 h/week per 5 clients

## Deliberately deferred (don't build yet)

- Multi-tenant single instance / client logins — instance-per-client is fine
  to ~15 clients.
- Billing automation, dunning — manual invoices until ~5 clients.
- Status pages, SMS alerts, SLA reporting — sell first, build on request.
- Recorder / test generator — big lever, but only after demand is proven.
