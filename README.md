# Kraken

Pirate Bay search and download UI.

## Features

- Search The Pirate Bay
- Download torrents
- Web-based UI

## Installation

```bash
# Clone the repo
git clone https://github.com/blaspat/kraken.git
cd kraken

# Install dependencies
pip3 install -r requirements.txt
```

## Usage

```bash
python3 app.py
```

## Configuration

`config.json` (untracked by git):

```json
{
  "password": "…login password…",
  "secret_key": "…Flask session secret…",
  "port": 8098,
  "default_dir": "~/Downloads",
  "download_dirs": ["~/Downloads"]
}
```

`port` is optional and defaults to `8098` if omitted.

## Access

- **URL:** `http://127.0.0.1:<port>` — port from `config.json` (default **8098**)

## License

Personal use only.
