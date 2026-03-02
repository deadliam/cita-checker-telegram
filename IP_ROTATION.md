# IP Rotation Configuration

The bot now supports automatic IP rotation on each run using proxy services. This helps avoid getting blocked when making frequent appointment checks.

## Configuration

Add proxy settings to your `values.json` file:

### Option 1: Single Proxy URL
```json
{
  "proxy_config": {
    "proxy_url": "http://proxy.example.com:8080"
  }
}
```

### Option 2: Rotating Proxy List
```json
{
  "proxy_config": {
    "proxy_list": [
      "http://proxy1.example.com:8080",
      "http://proxy2.example.com:8080",
      "http://proxy3.example.com:8080"
    ]
  }
}
```

## Proxy Format

The bot supports all standard proxy formats:

- **HTTP/HTTPS**: `http://proxy.example.com:8080` or `https://proxy.example.com:8080`
- **SOCKS4**: `socks4://proxy.example.com:1080`
- **SOCKS5**: `socks5://proxy.example.com:1080`
- **With authentication**: `http://username:password@proxy.example.com:8080`

## How It Works

1. On each bot run, a proxy is selected:
   - If using a proxy list, a random proxy is chosen
   - If using a single proxy URL, that proxy is always used
2. The proxy is passed to the Chrome browser via `--proxy-server=` flag
3. All HTTP requests from the bot will route through the selected proxy
4. The selected proxy is logged for debugging purposes

## Recommended Services

- **ProxyMesh**: Rotating residential proxies
- **Bright Data**: Large proxy network
- **Oxylabs**: Residential & datacenter proxies
- **SmartProxy**: Affordable rotating proxies

## Example with Rotating Proxies

```json
{
  "url": "https://...",
  "region": "Madrid",
  "check_interval_seconds": 600,
  "telegram_bot_token": "...",
  "proxy_config": {
    "proxy_list": [
      "http://user:pass@proxy1.service.com:8080",
      "http://user:pass@proxy2.service.com:8080",
      "http://user:pass@proxy3.service.com:8080"
    ]
  }
}
```

## Logging

When a proxy is configured, the bot logs:
```
Using proxy for this run: http://proxy.example.com:8080
```

This helps you verify which proxy was selected for each run.
