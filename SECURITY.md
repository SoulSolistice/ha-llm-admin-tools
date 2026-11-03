# Security and privacy reports

Only the latest version on `main` gets fixes.

**Never put the value itself in a report**, in public or in private: not a password, token,
address, coordinate or name that got through. Describe it instead, for example "a public IP
address followed by a port, in the result of `get_system_log`". If it was a credential,
change it, as it has already reached the model provider of your client.

## A way around a limit: report it privately

Use [private vulnerability reporting](https://github.com/SoulSolistice/ha-llm-admin-tools/security/advisories/new)
if a tool gets past what the settings should hold, for example if it:

- shows or changes something hidden or protected, or tells it apart from what does not exist;
- makes a change the settings do not allow;
- gets a client to read something withheld, on purpose.

Say which version, which tool, which settings, and what happened instead of a refusal.

## A value that slipped through: open an issue

Open an [issue](https://github.com/SoulSolistice/ha-llm-admin-tools/issues) if a value that
redaction should have withheld was shown by accident. Say which version, which tool and field,
which category it belongs to, and what the text around it looked like, with the value replaced.

## Not a bug

These are [known limits](README.md#6-limits-and-caveats):

- Anything done with the access token through another route, such as the REST API or another
  MCP endpoint. Home Assistant does not scope tokens.
- What a template reads while **Allow templates** is on.
- A secret under an ordinary name, in free text, or encoded, as listed in the README.
