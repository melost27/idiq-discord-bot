# Bot IDIQ → Discord · LM Sterling

Cada **30 segundos** revisa tu **IDIQ Partner Portal → Member Status** y **solo escribe en Discord cuando hay actividad**:

- **🆕 Nueva cuenta IdentityIQ**: nombre, apellido, teléfono, fecha y si el **Shared View** está activo.
  - **✅ READY**: el Shared View dice **View** (ya puedes ver el reporte).
  - **⏳ NO READY**: dice **No Consent**. El aviso le pide al equipo que **primero confirme si el cliente puede ver sus puntajes** en su cuenta de IdentityIQ. Si no los ve, el cliente debe llamar a **IdentityIQ al 877-875-4347** para verificar su información. Incluye los 2 mensajes listos para copiar y mandar al cliente.
- **✅ Cuenta READY**: cuando alguien que estaba en "No Consent" ya pasó a **View**.

Solo lee nombre, apellido, teléfono, fecha, status y Shared View. **Nunca** lee ni manda tarjeta, SSN, email ni dirección.

No manda mensajes de "bot conectado", ni de "sin cambios", ni de errores. La primera vez guarda las cuentas que ya existen **sin avisar** y desde ahí solo avisa lo nuevo.

Inicia sesión una sola vez y mantiene la sesión abierta; cada 30 segundos solo refresca la tabla. Si la sesión vence, vuelve a entrar solo.

---

## Paso 1 — Crear el webhook en Discord (2 min)

1. En tu servidor de Discord, pasa el mouse sobre el canal donde quieres los avisos (ej. `#identityiq`) → ⚙️ **Editar canal**.
2. **Integraciones** → **Webhooks** → **Nuevo webhook**.
3. Ponle de nombre `IDIQ Bot` → **Copiar URL del webhook**. Guárdala: es tu `DISCORD_WEBHOOK_URL`.

> Cualquiera con esa URL puede escribir en el canal. No la compartas.

## Paso 2 — Subir el código a GitHub (5 min)

1. Entra a github.com → **New repository** → nombre `idiq-discord-bot` → **Private** → Create.
2. **uploading an existing file** → arrastra estos archivos: `bot.py`, `requirements.txt`, `Dockerfile`, `README.md` → **Commit changes**.
   (El `.env.example` es solo de referencia. **No** subas tu contraseña a GitHub.)

## Paso 3 — Ponerlo a correr en Railway (10 min, ~$5/mes)

1. Entra a railway.com → inicia sesión con GitHub → **New Project** → **Deploy from GitHub repo** → elige `idiq-discord-bot`.
   Railway detecta el `Dockerfile` solo.
2. En el servicio → pestaña **Variables** → agrega:

   | Variable | Valor |
   |---|---|
   | `IDIQ_USER` | tu usuario del Partner Portal |
   | `IDIQ_PASSWORD` | tu contraseña del Partner Portal |
   | `DISCORD_WEBHOOK_URL` | la URL del Paso 1 |
   | `CHECK_EVERY_SEC` | `30` |
   | `DISCORD_MENTION` | *(opcional)* `@here` para que suene a todos |

3. Haz clic derecho en el servicio → **Attach Volume** → Mount path: **`/data`**.
   Esto es la "memoria" del bot. Sin el volumen, cada vez que Railway reinicie, olvida quién ya avisó.
4. **Deploy**. En **Logs** (no en Discord) debes ver `Iniciando sesion en el portal…`, `Primera corrida: N cuentas guardadas sin avisar.` y luego `Bot activo.` una vez por hora.
   Discord queda en silencio hasta que entre una cuenta nueva.

## Opciones

| Variable | Qué hace |
|---|---|
| `FIRST_RUN_NOTIFY_DAYS=2` | La primera vez, también avisa las cuentas de los últimos 2 días (para probar que llega bien). |
| `NOTIFY_WHEN_READY=0` | Deja de avisar cuando alguien pasa de No Consent a View. |
| `NOTIFY_ERRORS=1` | Avisa en Discord si el bot no puede leer el portal por ~5 min seguidos (por defecto está apagado). |

## Si algo falla

- Por defecto el bot **no** manda errores a Discord; revisa la pestaña **Logs** en Railway. Si quieres que avise cuando se cae, pon `NOTIFY_ERRORS=1`.
- **"No pude iniciar sesion"**: revisa usuario y contraseña en Variables. Si cambiaste la contraseña del portal, cámbiala también aquí.
- **"La columna 'X' no aparece"**: en Member Status, en el ⚙️ de columnas, vuelve a activar *First Name, Last Name, Phone, Shared View*.
- **El portal bloquea el servidor** (IDIQ usa protección anti-bots): si ves errores de carga o páginas en blanco en los Logs, el bot también puede correr en tu computadora con Docker Desktop:
  `docker build -t idiq-bot . && docker run -d --restart always --env-file .env -v idiq-data:/data idiq-bot`
  (copia `.env.example` como `.env` y llénalo).
