"""Creator settings over a private authenticated HTTP control channel.

The caller must be an authorized Core proxy. Runtime persistence is authoritative.
This endpoint is independent of delivery queues and never accepts a workspace scope.
"""
import asyncio
import hmac
import json
from starlette.responses import JSONResponse


class SettingsConflict(Exception):
    pass


class MaestroSettingsHTTP:
    def __init__(self, *, token, binding, schema, read, save, validate):
        if not token or len(token) < 32:
            raise ValueError('SETTINGS_TOKEN_REQUIRED')
        self.token, self.binding, self.schema = token, binding, schema
        self.read, self.save, self.validate = read, save, validate

    async def __call__(self, scope, receive, send):
        headers = scope.get('headers', [])
        tokens = [v for k, v in headers if k.lower() == b'maestro-settings-authorization']
        if len(tokens) != 1 or not hmac.compare_digest(tokens[0], self.token.encode()):
            await JSONResponse({'error': {'code': 'UNAUTHENTICATED'}}, status_code=401)(scope, receive, send)
            return
        try:
            body = bytearray()
            async with asyncio.timeout(5):
                while True:
                    part = await receive()
                    if part['type'] != 'http.request':
                        raise ValueError()
                    body.extend(part.get('body', b''))
                    if len(body) > 20000:
                        raise ValueError()
                    if not part.get('more_body'):
                        break
            payload = json.loads(body)
            if not isinstance(payload, dict) or payload.get('binding') != self.binding:
                raise ValueError()
            operation = payload.get('operation')
            if operation == 'read' and set(payload) == {'binding', 'operation'}:
                result = await self.read()
            elif operation == 'save' and set(payload) == {'binding', 'operation', 'version', 'values'}:
                version = payload['version']
                if type(version) is not int or version < 0:
                    raise ValueError()
                self.validate(payload['values'])
                result = await self.save(version, payload['values'])
            else:
                raise ValueError()
            response = JSONResponse({'binding': self.binding, 'schema': self.schema, **result})
        except SettingsConflict:
            response = JSONResponse({'error': {'code': 'SETTINGS_CONFLICT'}}, status_code=409)
        except (ValueError, TypeError, KeyError, TimeoutError):
            response = JSONResponse({'error': {'code': 'INVALID_SETTINGS'}}, status_code=400)
        except Exception:
            response = JSONResponse({'error': {'code': 'SETTINGS_UNAVAILABLE'}}, status_code=503)
        await response(scope, receive, send)
