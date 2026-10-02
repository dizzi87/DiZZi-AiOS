# Big Dizzi

Big Dizzi's original code is released under the [MIT License](LICENSE). The Brain incorporates or adapts Nate Herk's AIS-OS material under its separate [MIT notice](big_dizzi/static/brain/AIS-OS-LICENSE.txt). The bundled Brain dependencies retain their [third-party notices](big_dizzi/static/brain/THIRD-PARTY-NOTICES.txt). The top-level licence does not replace those notices.

Big Dizzi is an owner-operated application that uses an approved external Markdown Core, Codex app-server for reasoning and bounded engineering, and an optional separately configured Little Dizzi client. Private Core content, user data, local configuration, runtime records, credentials and infrastructure setup are not part of this repository.

## Setup boundaries

Use Python 3.12 or newer and install `requirements.txt` in an isolated environment. Copy `big_dizzi/config.example.json` to an untracked `big_dizzi/config.json`; replace every example path and model with a reviewed value. The selected Core root must contain the expected control routes, and Brain source paths must be explicitly approved. Configure `DIZZI_LITTLE_SSH_HOST` only when the restricted Little Dizzi capability is available. Keep gateway credentials and SSH identity outside Git.

The SIWC code follows [OpenAI's OSS sign-in guide](https://developers.openai.com/siwc/token-sharing-open-source/sign-in). It registers `Big Dizzi` using a distinct persistent host ID, PKCE, state and nonce; validates the OpenAI ID token and granted plan-use scope; then stores app-owned credentials in a protected directory. `python3 -m big_dizzi.siwc sign-in` opens the browser for the owner's consent. Sign-in alone does not prove plan inference; a real Codex app-server turn and refresh must also succeed. The Codex provider mode reads only this app's SIWC token; it has no automatic API-key fallback. Keep the SIWC credential directory outside Git and separate from Codex desktop auth.

Hosted mode is opt-in through a `hosted` configuration object containing `public_origin`, `access_issuer`, `access_audience` and `owner_sub`. It verifies Cloudflare Access's signed application JWT and binds it to an expiring first-party session. The browser enters at `/login`; Big Dizzi remains bound to loopback behind the planned tunnel. Cloudflare Access identity allow-list, authenticator MFA and Tunnel protection must be configured and accepted separately. This tree does not configure them.

Run the standalone boundary tests with `python3 -m unittest big_dizzi.tests.test_brain big_dizzi.tests.test_hosted_security -q`. Private implementation and acceptance records are excluded from this repository.
