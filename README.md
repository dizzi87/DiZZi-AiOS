# Big Dizzi — publication candidate

This is a source-only, fresh-history candidate for an owner-controlled open-source release. It has not been published or given a final top-level licence. The proposed licence is MIT, subject to owner approval. Nate Herk's AIS-OS licence and the bundled Brain dependency notices are preserved under `big_dizzi/static/brain/`.

Big Dizzi is an owner-operated application that uses an approved external Markdown Core, Codex app-server for reasoning and bounded engineering, and an optional separately configured Little Dizzi client. Core content, runtime records, credentials and infrastructure setup are not part of this tree.

## Setup boundaries

Use Python 3.12 or newer and install `requirements.txt` in an isolated environment. Copy `big_dizzi/config.example.json` to an untracked `big_dizzi/config.json`; replace every example path and model with a reviewed value. The selected Core root must contain the expected control routes, and Brain source paths must be explicitly approved. Configure `DIZZI_LITTLE_SSH_HOST` only when the restricted Little Dizzi capability is available. Keep gateway credentials and SSH identity outside Git.

The SIWC code is documented at [OpenAI's OSS sign-in guide](https://developers.openai.com/siwc/token-sharing-open-source/sign-in). It registers `Big Dizzi` using a distinct persistent host ID, PKCE, state and nonce; validates the OpenAI ID token and granted plan-use scope; then stores app-owned credentials in a protected directory. `python3 -m big_dizzi.siwc sign-in` opens the browser for the owner's consent. This candidate has **not** undergone owner consent or live plan inference. The Codex provider mode reads only this app's SIWC token; it has no automatic API-key fallback. Keep the SIWC credential directory outside Git and separate from Codex desktop auth.

Hosted mode is opt-in through a `hosted` configuration object containing `public_origin`, `access_issuer`, `access_audience` and `owner_sub`. It verifies Cloudflare Access's signed application JWT and binds it to an expiring first-party session. The browser enters at `/login`; Big Dizzi remains bound to loopback behind the planned tunnel. Cloudflare Access identity allow-list, authenticator MFA and Tunnel protection must be configured and accepted separately. This tree does not configure them.

Run the standalone boundary tests with `python3 -m unittest big_dizzi.tests.test_brain big_dizzi.tests.test_hosted_security -q`. The private implementation repository retains additional convergence tests and local acceptance evidence; those records are intentionally excluded from this candidate.
