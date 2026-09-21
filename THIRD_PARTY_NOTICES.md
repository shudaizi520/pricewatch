# Third-Party Notices

PriceWatch depends on the following directly imported open-source packages. Production version
constraints are in `constraints.txt`; CI generates a transitive dependency and license inventory
before any release. That inventory must be reviewed against the built image.

The image also installs Playwright's Chromium headless-shell and its system libraries. Chromium
contains BSD-style and other third-party license notices that ship with the upstream browser
distribution; the image build should be checked using the CI license inventory before release.

| Package | License | Source |
|---|---|---|
| Alembic | MIT | https://github.com/sqlalchemy/alembic |
| Apprise (Feishu delivery) | MIT | https://github.com/caronc/apprise |
| APScheduler | MIT | https://github.com/agronholm/apscheduler |
| argon2-cffi | MIT | https://github.com/hynek/argon2-cffi |
| Beautiful Soup | MIT | https://www.crummy.com/software/BeautifulSoup/ |
| cryptography | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography |
| FastAPI | MIT | https://github.com/fastapi/fastapi |
| HTTPX | BSD-3-Clause | https://github.com/encode/httpx |
| itsdangerous | BSD-3-Clause | https://github.com/pallets/itsdangerous |
| Jinja | BSD-3-Clause | https://github.com/pallets/jinja |
| lxml | BSD-3-Clause | https://github.com/lxml/lxml |
| Playwright Python | Apache-2.0 | https://github.com/microsoft/playwright-python |
| Pydantic Settings | MIT | https://github.com/pydantic/pydantic-settings |
| python-multipart | Apache-2.0 | https://github.com/Kludex/python-multipart |
| SQLAlchemy | MIT | https://github.com/sqlalchemy/sqlalchemy |
| Typer | MIT | https://github.com/fastapi/typer |
| Uvicorn | BSD-3-Clause | https://github.com/encode/uvicorn |
