# Contributing

## Running the tests

The tests use Home Assistant's own test package, pinned to the Home Assistant version the integration targets. Home Assistant only runs on Linux and macOS, so on Windows run them in Docker:

```
docker build -f Dockerfile.test -t voice-assistant-manager-test .
docker run --rm -v "$PWD:/src" voice-assistant-manager-test
```

On Linux or macOS with Python 3.14 you can run them directly:

```
python -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pytest
```

## Licence of contributions

The project is licensed under the GNU Affero General Public License, version 3 or (at your option) any later version (`AGPL-3.0-or-later`, see [LICENSE](LICENSE)). By sending a contribution you agree that it is licensed under the same terms. New source files start with the two lines every existing file has:

```
SPDX-FileCopyrightText: 2026 rodavrow
SPDX-License-Identifier: AGPL-3.0-or-later
```

in that file type's comment syntax.
