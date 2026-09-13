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

### Open issue: the redemption call has never succeeded

Between 2026-09-04 08:10Z and 2026-09-13 00:13Z the service attempted
redemption 413 times and every one returned `access_code_redemption_error`.
In the same window a manual browser redemption worked first try.

Ruled out by measurement:

* **Datacenter IP** — reproduced identically from a residential IP.
* **Dead session** — the data layer reports `isLoggedIn: true` throughout.
* **Stale bot cookies** — walking the redeem and activate pages first (which is
  what a browser does) did not change the outcome, and the `datadome` cookie
  was neither rejected nor reissued.
* **API drift** — the front-end chunk hash is unchanged, and NYT answers with
  its own domain errors, so the request is understood.

Two hypotheses remain, and they are not yet distinguishable:

1. The request is missing something the browser sends.
2. There was a temporary block or cooldown that happened to lapse right when
   the manual redemption was tried.

The manual redemption landed about one minute before the next scheduled tick,
so the automation never got a clean attempt afterwards. The decisive test is
the next natural expiry: if the automated attempt succeeds then, hypothesis 2
holds and the request was always fine.

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
