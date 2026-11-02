# OngakuDownloader

Aplicación web local para organizar y descargar, para uso personal, pistas de playlists de YouTube y YouTube Music.

## Inicio rápido

1. Haz doble clic en `start.bat`.
   En el primer inicio se instala el proveedor local de PO Tokens recomendado por yt-dlp. Después la interfaz se abre en Chrome.
2. Pega la URL de una playlist pública.
3. Revisa la selección, el formato, la portada y la carpeta de destino.
4. Pulsa **Descargar seleccionadas**.

La aplicación abre `http://127.0.0.1:8766` y solo escucha en tu equipo.

Para playlists públicas deja seleccionada **Sin cookies**. Usa Firefox únicamente si la playlist es privada, tiene restricción de edad o YouTube exige iniciar sesión. No se recomienda intentar leer las cookies cifradas de Chrome.

## Calidad

- **Opus:** prefiere la mejor fuente Opus disponible.
- **M4A:** prefiere la mejor fuente M4A disponible.
- **MP3:** parte de la mejor fuente de audio disponible y convierte con VBR de máxima calidad. No fuerza 320 kbps.

Para convertir formatos, incrustar portadas o redimensionarlas se necesita FFmpeg. El programa comprueba su disponibilidad al iniciar.

## Notas

- Usa la herramienta únicamente con contenido que tengas derecho a descargar.
- Las letras dependen de subtítulos publicados o generados por YouTube.
- Las letras encontradas se incrustan dentro del propio archivo: etiqueta `USLT` en MP3, `©lyr` en M4A y `LYRICS` en Opus. Los `.vtt` temporales se eliminan.
- La elección de carpeta se realiza con el botón **Elegir carpeta** y el selector nativo de Windows.
- Los nombres nuevos no incluyen el ID técnico de YouTube. Al volver a usar una carpeta de playlist, la aplicación también limpia esos IDs de archivos antiguos.
- La búsqueda e incrustación de letras se ejecuta en segundo plano mientras continúan las descargas de audio, manteniendo una sola consulta de letras simultánea para reducir errores `429`.
- Si YouTube limita las letras, el audio conserva el estado de descarga completada y la letra aparece solo como aviso; nunca invalida la canción.
- Un error intermitente de PO Token o HTTP 403 se reintenta automáticamente con `mweb` y después con el flujo HLS de `web_safari`.
- Si YouTube responde `Video unavailable` con todos los clientes, el video original fue retirado o bloqueado. La aplicación lo identifica como un error individual y continúa con el resto de la cola.
- Aumentar demasiado la concurrencia puede provocar limitaciones temporales de YouTube. El valor recomendado es 2.



## Privacidad y seguridad

- Servicio local en 127.0.0.1:8766; no publicar este servidor en Internet.
- No incluye cuentas, analítica ni telemetría propia. Las consultas y descargas contactan con YouTube/Google; las portadas se cargan desde sus servidores.
- La lectura de cookies está desactivada por defecto. Al elegir un navegador, yt-dlp accede a su sesión local para las peticiones. Úsalo solo si es necesario, preferiblemente con una cuenta dedicada.
- Solo acepta enlaces HTTPS de YouTube. Bloquea solicitudes de otros sitios, hosts externos, carpetas de red y cuerpos que no sean objetos JSON.
- Los procesos de yt-dlp ignoran configuraciones externas del usuario. Se siguen utilizando el proveedor de tokens y EJS, dependencias externas necesarias para YouTube.
- Descargas, cookies, secretos, registros y entornos se excluyen de Git. No agregues archivos personales manualmente.
- El instalador descarga dependencias desde PyPI, npm y el repositorio oficial del proveedor. Mantén las dependencias actualizadas; esta revisión no garantiza ausencia total de vulnerabilidades.
- El icono de tres barras se anima mientras la pestaña está visible y respeta movimiento reducido. Su visualización depende del navegador.

## Desarrollo

Requiere Python 3.12+, Node.js, npm, Git y FFmpeg/FFprobe en el PATH.
Pruebas: python -m unittest discover -s tests -v.

El proveedor oficial 2.0.2 se instala con los manifiestos revisados de runtime/, sin las herramientas de lint del proyecto externo. Se conserva su licencia GPL-3.0-only y se ejecuta como proceso separado. Sus dependencias y las de Python fueron auditadas antes de la publicación; deben volver a revisarse al actualizar.

Referencias de seguridad: https://flask.palletsprojects.com/en/stable/web-security/ y https://github.com/yt-dlp/yt-dlp#configuration.
