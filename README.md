# Unified Tracker v2 — Quick Start

## Formas de arrancar

| Archivo | Cuándo usarlo |
|---|---|
| `start.bat` | Modo consola visible. Ves los prints en tiempo real. Útil para debugging. |
| `start_hidden.vbs` | **Modo 8h desatendido.** Sin ventana. Doble-click → arranca silencioso. |
| `install_startup.vbs` | Autoarranque al iniciar sesión de Windows. Ejecutar **una vez**. |
| `uninstall_startup.vbs` | Quita el autoarranque. |

## Cómo funciona el autoarranque silencioso

`start_hidden.vbs` invoca `start_hidden_wrapper.bat` con `WindowStyle=0`
(oculto). El `.bat` detecta `pythonw.exe`, lo arranca con `start /B`,
y luego prueba que el puerto 5050 esté escuchando. **Todo el output
queda en `data/tracker_stdout.log`**, así que si algo falla lo puedes
leer sin abrir consola.

## Para usarlo normalmente

1. **Doble click en `start_hidden.vbs`** → arranca sin ventana.
2. Espera ~5 segundos para que Flask binde.
3. Abre `http://127.0.0.1:5050` en el navegador.
4. Para detener: botón Stop en la UI, o **taskkill /F /PID <pid>**
   (busca el `pythonw.exe` en Task Manager → Details).

## Para dejarlo siempre corriendo

1. Una vez: doble click en `install_startup.vbs`.
2. Listo. Cada vez que inicies sesión, el tracker arrancará solo.

## Si algo falla

- Abre el panel "📊 Diagnostics & Live Logs" en la UI — success rate, zombies, log en vivo.
- Log persistente: `data/tracker.log`.
- Errores de arranque: `data/tracker_stdout.log` (escrito por el `.bat`).

## Tests de regresión

```bash
python test_regression.py
```

Verifica que los 3 bugs originales (zombi Zitobox, Carnival listener leak, log append) siguen corregidos.