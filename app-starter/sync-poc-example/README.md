# sync-poc-example

A worked example of the sync skeleton wired to a concrete pair of Pydantic
models, so you can see which lines move when you drop your own in.

    models.py       stands in for YOUR models module
    settings.py     configuration, via pydantic-settings
    sync_poc.py     the sync itself
    .env.example    copy to .env

`models.py` is a stand-in. In your project it is whatever module already holds
`LedgerEntry` and `PostingRequest`, and the only change to `sync_poc.py` is the
import at the top.

## Run it

    pip install -e ../client-core
    pip install pydantic-settings
    cp .env.example .env      # then fill it in

    python sync_poc.py            # dry run: print what would be posted
    python sync_poc.py --post     # actually post
    python sync_poc.py --limit 5

Dry run is the default.

## Configuration

Copy `.env.example` to `.env` and fill it in. `.env` is gitignored; keep it
that way.

    pip install pydantic-settings
    cp .env.example .env

Real environment variables override `.env`, so a container or a systemd
`EnvironmentFile` wins without anyone deleting the developer file.

Missing values are reported together and the process exits 3:

    configuration is incomplete:

      SRC_URL: Field required
      SRC_CLIENT_SECRET: Field required

    Set them in .env or the environment. See .env.example.

`extra="forbid"` means a typo in `.env` is an error rather than a line that
silently does nothing. The two secrets are `SecretStr`, so they repr as
`********` and only yield their value at the one call site that needs them.

Defaults for paths, timeouts and retry budgets live in `settings.py` and can be
overridden the same way.
