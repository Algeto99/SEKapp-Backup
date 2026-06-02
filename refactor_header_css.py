import os
import re

templates_dir = "/Users/rcanton/Library/CloudStorage/GoogleDrive-roberto.j.canton@gmail.com/My Drive/SMT/git/secapp/monolith/templates"

classes_to_remove = [
    r'\.header-nav',
    r'\.nav-content',
    r'\.nav-links',
    r'\.nav-link',
    r'\.nav-link:hover',
    r'\.user-greeting',
    r'\.user-name',
    r'\.admin-badge',
    r'body\.light-mode\s+\.header-nav',
    r'body\.light-mode\s+\.nav-link',
    r'body\.light-mode\s+\.nav-link:hover',
    r'body\.light-mode\s+\.user-greeting',
    r'body\.light-mode\s+\.user-name'
]

modified_files = []

for root, _, files in os.walk(templates_dir):
    for f in files:
        if not f.endswith(".html"):
            continue
        path = os.path.join(root, f)
        with open(path, "r", encoding="utf-8") as file:
            content = file.read()
        
        original_content = content
        
        # Remove the CSS classes
        for cls in classes_to_remove:
            pattern = re.compile(r'^\s*' + cls + r'\s*\{[^}]+\}', re.MULTILINE)
            content = pattern.sub('', content)

        # Some templates might define inline `style="color: #f87171;"` for `/logout`, let's remove it if it has class="nav-link"
        content = re.sub(r'class="nav-link"\s+style="color:\s*#f87171;?"', 'class="nav-link"', content)
        content = re.sub(r'style="color:\s*#f87171;?"\s+class="nav-link"', 'class="nav-link"', content)
        
        # Add the stylesheet if not present
        if 'css/header.css' not in content and '<head>' in content:
            link_tag = '    <link rel="stylesheet" href="{{ url_for(\'static\', filename=\'css/header.css\') }}">\n'
            content = content.replace('</head>', link_tag + '</head>')
            
        if content != original_content:
            with open(path, "w", encoding="utf-8") as file:
                file.write(content)
            modified_files.append(f)

print(f"Modified {len(modified_files)} files: {', '.join(modified_files)}")
