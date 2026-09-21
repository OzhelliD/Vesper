#!/usr/bin/env python3
"""
Validación de Vesper v2.0 antes de deploy a Railway

Verifica que todo esté listo para producción.
"""

import os
import sys
import re
from pathlib import Path

def check(condition, message, is_error=False):
    """Helper para validación"""
    if condition:
        print(f"✅ {message}")
        return True
    else:
        print(f"❌ {message}")
        return not is_error

def main():
    print("\n" + "="*70)
    print("🔍 VALIDACIÓN VESPER v2.0 PARA RAILWAY")
    print("="*70 + "\n")
    
    errors = []
    warnings = []
    
    # 1. Archivos necesarios
    print("📁 Verificando archivos...")
    required_files = [
        'main.py',
        'requirements.txt',
        '.env.example',
        'railway.toml',
        'Dockerfile',
        'README.md',
        'DEPLOYMENT.md',
        'static/index.html'
    ]
    
    for f in required_files:
        exists = Path(f).exists()
        if not exists:
            errors.append(f"Falta archivo: {f}")
        check(exists, f"  {f}")
    
    # 2. Validar main.py
    print("\n🐍 Validando main.py...")
    with open('main.py', 'r') as f:
        main_content = f.read()
    
    # Verificar que morning routine fue removido
    if 'wake_up_routine' in main_content:
        errors.append("morning routine aún en main.py")
        check(False, "  morning routine eliminado")
    else:
        check(True, "  morning routine eliminado")
    
    if '_routine_state' in main_content:
        errors.append("_routine_state aún en main.py")
        check(False, "  _routine_state eliminado")
    else:
        check(True, "  _routine_state eliminado")
    
    # Verificar que Flask app está presente
    if 'app = Flask' in main_content:
        check(True, "  Flask app inicializado")
    else:
        errors.append("Flask app no encontrado en main.py")
        check(False, "  Flask app inicializado")
    
    # Verificar imports
    essential_imports = [
        'from flask import',
        'from anthropic import',
        'import psycopg2',
    ]
    for imp in essential_imports:
        if imp in main_content:
            check(True, f"  {imp} presente")
        else:
            warnings.append(f"Import esperado no encontrado: {imp}")
            check(False, f"  {imp} presente", is_error=False)
    
    # 3. Validar requirements.txt
    print("\n📦 Validando requirements.txt...")
    with open('requirements.txt', 'r') as f:
        reqs = f.read().lower()
    
    essential_packages = [
        'flask',
        'anthropic',
        'psycopg2',
        'gunicorn',
        'apscheduler'
    ]
    for pkg in essential_packages:
        if pkg in reqs:
            check(True, f"  {pkg} en requirements.txt")
        else:
            errors.append(f"Dependencia faltante: {pkg}")
            check(False, f"  {pkg} en requirements.txt")
    
    # 4. Validar .env.example
    print("\n⚙️  Validando .env.example...")
    with open('.env.example', 'r') as f:
        env_content = f.read()
    
    if 'DATABASE_URL' in env_content:
        check(True, "  DATABASE_URL documentada")
    else:
        errors.append("DATABASE_URL no en .env.example")
        check(False, "  DATABASE_URL documentada")
    
    if 'ANTHROPIC_API_KEY' in env_content:
        check(True, "  ANTHROPIC_API_KEY documentada")
    else:
        errors.append("ANTHROPIC_API_KEY no en .env.example")
        check(False, "  ANTHROPIC_API_KEY documentada")
    
    # 5. Validar Dockerfile
    print("\n🐳 Validando Dockerfile...")
    with open('Dockerfile', 'r') as f:
        docker_content = f.read()
    
    if 'FROM python' in docker_content:
        check(True, "  Base image presente")
    else:
        errors.append("Dockerfile no tiene base image")
        check(False, "  Base image presente")
    
    if 'gunicorn' in docker_content.lower() or 'CMD' in docker_content:
        check(True, "  Servidor configurado")
    else:
        warnings.append("Servidor no explícitamente configurado en Dockerfile")
        check(False, "  Servidor configurado", is_error=False)
    
    # 6. Validar railway.toml
    print("\n🚂 Validando railway.toml...")
    with open('railway.toml', 'r') as f:
        rail_content = f.read()
    
    if 'healthcheckPath' in rail_content:
        check(True, "  Health check configurado")
    else:
        warnings.append("Health check no configurado en railway.toml")
        check(False, "  Health check configurado", is_error=False)
    
    if 'startCommand' in rail_content or 'gunicorn' in rail_content.lower():
        check(True, "  Start command presente")
    else:
        errors.append("Start command no en railway.toml")
        check(False, "  Start command presente")
    
    # 7. Sintaxis Python
    print("\n✨ Validando sintaxis Python...")
    import subprocess
    result = subprocess.run(
        [sys.executable, '-m', 'py_compile', 'main.py'],
        capture_output=True,
        text=True
    )
    if result.returncode == 0:
        check(True, "  main.py compila sin errores")
    else:
        errors.append(f"Errores de sintaxis en main.py: {result.stderr}")
        check(False, "  main.py compila sin errores")
    
    # 8. Revisar endpoints críticos
    print("\n🔌 Verificando endpoints críticos...")
    critical_endpoints = [
        r"@app\.route\(['\"]\/api\/health['\"]",
        r"@app\.route\(['\"]\/api\/chat['\"]",
        r"@app\.route\(['\"]\/api\/financial",
    ]
    
    for endpoint in critical_endpoints:
        if re.search(endpoint, main_content):
            check(True, f"  Endpoint {endpoint.split('/')[-1]} presente")
        else:
            warnings.append(f"Endpoint posiblemente faltante: {endpoint}")
    
    # Resultado final
    print("\n" + "="*70)
    
    if errors:
        print(f"❌ ERRORES ({len(errors)}):\n")
        for err in errors:
            print(f"   • {err}")
        print("\n⚠️  NO está listo para producción\n")
        return 1
    elif warnings:
        print(f"⚠️  ADVERTENCIAS ({len(warnings)}):\n")
        for warn in warnings:
            print(f"   • {warn}")
        print("\n✅ Listo para producción (pero revisa las advertencias)\n")
        return 0
    else:
        print("✅ TODO VERIFICADO - LISTO PARA RAILWAY\n")
        print("Próximos pasos:")
        print("  1. git push")
        print("  2. Conectar con Railway")
        print("  3. Configurar variables en Railway")
        print("  4. ¡Listo!\n")
        return 0

if __name__ == '__main__':
    sys.exit(main())
