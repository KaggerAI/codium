import os
import glob

def main():
    directory = r'c:\Users\harsh\codium'
    html_files = glob.glob(os.path.join(directory, '*.html'))
    injection_tag = '  <script src="scale_ui.js"></script>\n'
    
    for filepath in html_files:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Skip if already injected
        if 'scale_ui.js' in content:
            print(f"Skipping {os.path.basename(filepath)}, already injected.")
            continue
            
        # Inject script just before </head>
        if '</head>' in content:
            content = content.replace('</head>', injection_tag + '</head>')
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"Injected into {os.path.basename(filepath)}")
        else:
            print(f"Warning: No </head> found in {os.path.basename(filepath)}")

if __name__ == "__main__":
    main()
