from ui.ehal_mqtt_form import render_mqtt_connection_form

# Stands in for the app shell: app.py shows queued flash notices before the page.
from ui.flash import render_flash_notices

render_flash_notices()

render_mqtt_connection_form(form_key="t_mqtt")
