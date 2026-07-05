# Unified Tracker v2 — Quick Start

## Formas de arrancar

| Archivo | Cuándo usarlo |
|---|---|
| `start.bat` | Modo consola visible. Ves los prints en tiempo real. Útil para debugging. |
| `start_hidden.vbs` | **Modo 8h desatendido.** Sin ventana. Doble-click → arranca silencioso, te avisa con un MsgBox. |
| `install_startup.vbs` | Autoarranque al iniciar sesión de Windows. Ejecutar **una vez**. |
| `uninstall_startup.vbs` | Quita el autoarranque. |

## Para usarlo normalmente

1. **Doble click en `start_hidden.vbs`** → arranca sin ventana.
2. Abre `http://127.0.0.1:5050` en el navegador.
3. Para detener: botón Stop en la UI, o en Task Manager → buscar "pythonw.exe" → End task.

## Para dejarlo siempre corriendo

1. Una vez: doble click en `install_startup.vbs`.
2. Listo. Cada vez que inicies sesión, el tracker arrancará solo.

## Si algo falla

- Abre el panel "📊 Diagnostics & Live Logs" en la UI — ves success rate, zombies, log en vivo.
- Log persistente: `data/tracker.log`.
- Crashes con `python.exe` (no `pythonw`): `data/tracker_stdout.log`.

## Tests de regresión

```bash
python test_regression.py
```

Verifica que los 3 bugs originales (zombi Zitobox, Carnival listener leak, log append) siguen corregidos.