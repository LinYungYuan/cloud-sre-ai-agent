# SRE Agent frontend

Independent Angular 22 standalone application for the SRE Agent web interface.

## Requirements

- Node.js `>=24.15.0 <25`
- npm 11

## Runtime configuration

The application fetches `/config.json` before Angular starts. Deployment must provide all three fields:

```json
{
  "apiBaseUrl": "/api/v1",
  "locale": "zh-TW",
  "timeZone": "Asia/Taipei"
}
```

Startup fails if the response is unavailable or invalid. Feature code should inject `RUNTIME_CONFIG` rather than hard-code an API base URL.

## Commands

```bash
npm start
npm test -- --watch=false
npm run build
```

Production build artifacts are written beneath `dist/frontend/`.
