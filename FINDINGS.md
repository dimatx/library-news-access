# Findings: what each MHL newspaper actually grants

Measured by replaying every flow by hand.

> **Correction (2026-09-02).** An earlier version of this file claimed only the
> Boston Globe was renewable and that NYT / WSJ / Washington Post were one-shot
> redemption codes. **That was wrong.** All four are rolling passes. The error
> is documented at the bottom because the way it happened is worth remembering.

## The passes

| Newspaper | `db_id` | Pass length | Renewal mechanism | Status |
|---|---|---|---|---|
| **Boston Globe** | 37577 | 72 hours | Resubmit the registration form | **Automated** (`ez_register`) |
| **New York Times** | 20528 | 24 hours | Redeem a bulk certificate against your account | **Automated** (`nyt_redeem`) |
| Wall Street Journal | 3969 | 3 days | "Log in as an existing user with the same username and password" | Not yet built |
| Washington Post | 3970 | 7 days | "Revisit this page and restart the process" | Not yet built |
| Eagle Tribune (NewsBank) | n/a | none | Card entry mints a throwaway browsing session | **Excluded, permanently** |

The library states each duration in plain English on its `/databases` page.

Eagle Tribune is the only genuine exclusion: there is no account and no
entitlement, just a session that dies with the tab. Nothing to keep alive.

## Boston Globe

1. `POST https://mhl.org/connect/37577` with `mhl-connect=`, `db_id=37577`,
   `card_number=<card>`.
   - Valid card -> `302` to `https://manage.bostonglobe.com/cs/reg/ez/mhl.aspx`
   - Invalid card -> `200` with `.validation-message`:
     *"Not a valid library card number. Please try again."*
2. That page is ASP.NET WebForms. Echo back `__VIEWSTATE`,
   `__VIEWSTATEGENERATOR`, `__EVENTVALIDATION` plus `txtFirst`, `txtLast`,
   `txtEmail`, `txtPassword`, `txtVerifyPassword`, and POST to `./mhl.aspx`.
   The submit `<button id="cmdSubmit">` has **no `name`**, so nothing extra is
   sent for it.
3. Success -> `302` off `manage.bostonglobe.com` to `bostonglobe.com`.

Submitting "Create Account" for an account that already exists is the intended
path; it re-ups the entitlement and does not reset the password.

## New York Times

Pure HTTP; no browser is required to *read* state. The redemption call,
however, has an unresolved problem — see the open issue at the end.

> **Correction (2026-09-13).** A previous version of this file claimed the
> library's code was one-redemption-per-account and therefore spent. **That was
> wrong.** A manual browser redemption on the same account and the same code
> succeeded on 2026-09-13 00:13:54Z, ten days after the first one. The evidence
> for the wrong claim was a browser showing "This code has already been
> redeemed" — which was simply the response to clicking Redeem a second time,
> moments after the first click had already worked.

1. `POST https://mhl.org/connect/20528` -> `302` to
   `https://nytimes.com/subscription/redeem/all-access?campaignId=8F978&gift_code=<code>`.
   The code is static: byte-identical across fresh sessions and across weeks.
2. The "Redeem" button is not a form post (`POST` to that page returns `405`).
   It navigates to `/activate-access/access-code?access_code=...`, which is a
   client-side route that calls Apollo against
   `https://samizdat-graphql.nytimes.com/graphql/v2`.
3. The mutation, lifted from `chunks/ActivateAccess-*.js`:

   ```graphql
   mutation redeemAccessCode($accessCode: String!, $campaignId: String!) {
     redeemAccessCode(redeemAccessCodeInput: {
       accessCode: $accessCode
       trackingMetadata: { key: "campaignId", value: $campaignId }
     }) { success subscriptionEndDate subscriptionDurationDays ... }
   }
   ```

   Required headers come from `window.__preloadedData.config.gqlRequestHeaders`
   on the redeem page: `nyt-app-type: project-vi`, `nyt-app-version`, and a
   static `nyt-token`. The browser additionally sends `x-pageview-id` (from
   client-side tracking, empty when unavailable) and `x-plid`; both look like
   telemetry.
4. `https://a.nytimes.com/svc/nyt/data-layer` reports live entitlement state.
   The library's subscription is identifiable by `campaignId` and by
   `subscriptionLabels` containing `BULK_CERT_REDEMPTION`, and carries a real
   `endDate` in ISO 8601 UTC. This is what drives renewal, rather than a timer.

   **`isLoggedIn` lives under `session`, not `user`.** And NYT keeps
   `hasActiveEntitlements` set for hours after a pass has really lapsed, still
   carrying the stale `endDate`, so the end date is authoritative.

**Login is never scripted.** `myaccount.nytimes.com/auth/login` returns `403`
with `Server: DataDome` and a risk score around 0.95 for any non-browser
client, from a home IP and a datacenter IP alike. The service instead reuses a
`cookies.txt` exported once from a logged-in browser. The redeem path itself is
not challenged: it returns `200` from both.

Redeeming while a pass is already live returns
`{"errors":[{"message":"access_code_already_redeemed"}]}` with HTTP `200`,
which is treated as "still active", not a failure.

### Resolved: the missing CheckAccessCode call

Between 2026-09-04 08:10Z and 2026-09-14 00:36Z every automated redemption
returned `access_code_redemption_error`, while browser redemptions on the same
account and code succeeded every time. A HAR capture of a working browser
redemption showed why: the browser issues **two** calls, in order.

```
POST /graphql/v2   CheckAccessCode   (query)    -> status READY_FOR_REDEMPTION
POST /graphql/v2   redeemAccessCode  (mutation) -> success: true
```

We only ever sent the mutation. Its body is otherwise effectively identical to
ours, so the absent preceding query is the substantive difference.

The captured request headers are now matched too: `x-pageview-id` and `x-plid`
(per-pageview tracking ids, regenerated per call in the same 24-character
url-safe form), `x-nyt-internal-meter-override`, the `sec-fetch-*` set, and
`referer`, which the browser sends as the **site root** rather than the
activate-access URL.

Ruled out by measurement along the way, so none of these need revisiting:

* **Datacenter IP** — reproduced identically from a residential IP.
* **Dead session** — the control query `ssoEmailDomainPassEligibility` returned
  the account email over the same cookies and headers, so samizdat honours our
  session; only this one mutation was refused.
* **Expired cookies** — the jar carries seven, including `NYT-MPS` (dead for 10
  days). Dropping them, and sending only the live ones, both changed nothing.
* **Stale bot cookies / API drift** — walking the redeem and activate pages
  first changed nothing, and the token, app version and endpoint all match the
  live page exactly.

Two wrong conclusions were reached before the HAR, both worth remembering:

1. *"The code is spent, one redemption per account."* Disproved within the hour
   by a successful browser redemption. The evidence was a browser showing "This
   code has already been redeemed" — which was the response to clicking Redeem
   a *second* time, moments after the first click had already worked.
   `CheckAccessCode` now reports the certificate is valid until **2031-04-21**.
2. *"USER_ALREADY_SUBSCRIBER is the blocker."* That came from
   `digitalGiftEligibility`, which answers for the **gift** path. The browser
   redeemed successfully while that query was reporting it.

Both mistakes shared a shape: inferring a cause from a single observation
without a control, when a control was cheaply available.

### Other NYT notes

* `redeemAccessCode` returns a `subscriptionEndDate` that has been seen to
  disagree with the subscription record NYT then creates (`04:00Z` against a
  true +24h). The published expiry is re-read from the account state instead.
* Redeeming while a pass is already live returns
  `{"errors":[{"message":"access_code_already_redeemed"}]}` with HTTP `200`,
  which is treated as "still active", not a failure.
* NYT keeps `hasActiveEntitlements` set for hours after a pass has really
  lapsed, still carrying the stale `endDate`, so the end date is authoritative.
* **`isLoggedIn` lives under `session`, not `user`.**

## How the earlier conclusion went wrong

I checked whether `gift_code` changed between requests. It did not, and I
concluded "static code, therefore one-shot redemption". A non-rotating code
only means the code is library-wide; the *pass* it mints is per-account and
expires.

The durations were written in plain English on `/databases`, which I had
already downloaded and grepped only for `/connect/(\d+)`. The answer was in a
file on disk the whole time.

**The lesson: measuring the mechanism is not the same as measuring the
semantics.** "Does this value change?" is a question about plumbing. "How long
does this entitlement last?" is the question that actually decided the
architecture, and the vendor had already answered it in prose.
