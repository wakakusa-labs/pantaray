<div align="center">
  <img src="frontend/assets/icons/Pantaray_icon_small.svg" width="96" alt="Pantaray icon">
  <h1>Pantaray</h1>
  <p><strong>The intelligence that moves first.</strong></p>
  <p>
    <img src="https://img.shields.io/badge/license-Apache--2.0-1a2332" alt="License: Apache-2.0">
    <img src="https://img.shields.io/badge/platform-macOS%20(Apple%20silicon)-1a2332" alt="Platform: macOS (Apple silicon)">
    <a href="https://github.com/wakakusa-labs/pantaray/actions/workflows/ci-backend.yml"><img src="https://github.com/wakakusa-labs/pantaray/actions/workflows/ci-backend.yml/badge.svg" alt="CI (backend)"></a>
    <a href="https://github.com/wakakusa-labs/pantaray/actions/workflows/ci-desktop.yml"><img src="https://github.com/wakakusa-labs/pantaray/actions/workflows/ci-desktop.yml/badge.svg" alt="CI (desktop)"></a>
  </p>
</div>

Pantaray is an intelligence that thinks and acts for you. It understands your work and shares a TODO
list with you.

- **It works through your list.** Add a TODO and Pantaray starts on it on its start date, carrying it
  through to the result.
- **It moves first.** From what you are working on, it finds the work that matters, takes it as far as
  it can, and reports back. Reply to carry it on.
- **You choose the AI.** Your own OpenAI, Anthropic, or Fireworks AI key, or your ChatGPT account.

## Install

Pantaray runs on macOS with Apple silicon. Download
[Pantaray.dmg](https://github.com/wakakusa-labs/pantaray/releases/latest/download/Pantaray.dmg) and
drag Pantaray into your Applications folder. Builds are signed and notarized, and the app keeps itself up to date.

## Getting started

1. Open **Settings → AI connection** and choose how to connect: your own API key or your ChatGPT
   account.
2. Press **Start recording** and allow the macOS permissions Pantaray asks for.
3. Pantaray reports in your TODO list as you work. To add a TODO from any app, press **Option+Space**.

## Privacy

With your permission, Pantaray records the apps you use, your window titles, the text on screen,
and what you type. The raw recording is an encrypted file on this Mac and is deleted after 48
hours. The summaries of your work made from it stay on this Mac.
Password managers, and sign-in and payment pages, are never recorded. Only what a suggestion or a
request needs is sent to the AI you chose; the recording file itself never leaves this Mac.
Pantaray has no analytics and no usage tracking.

## Bug reports

Please report bugs in [GitHub Issues](https://github.com/wakakusa-labs/pantaray/issues). We are not
accepting pull requests at this time.

## Security

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## License

Pantaray is licensed under the [Apache License 2.0](LICENSE). Third-party components bundled into
the released app are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
