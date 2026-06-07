import os

files = [
    "reporte_incidente.html",
    "encuesta_cliente.html",
    "supervision_puesto.html",
    "reporte_disciplinario.html",
    "log_de_patrullas.html",
    "registro_de_capacitaciones.html",
    "acta_visita_cliente.html",
    "planilla_vehicular.html",
    "planilla_motocicletas.html",
    "checklist_cumplimiento.html",
    "confiabilidad_equipos.html"
]

base_dir = "/Users/rcanton/Library/CloudStorage/GoogleDrive-roberto.j.canton@gmail.com/My Drive/SMT/git/secapp/monolith/templates"

for filename in files:
    filepath = os.path.join(base_dir, filename)
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            lines = f.readlines()
        
        with open(filepath, 'w') as f:
            for line in lines:
                if 'name="csrf_token"' in line and '{{ csrf_token() }}' in line:
                    print(f"Removed line from {filename}: {line.strip()}")
                    continue
                f.write(line)
