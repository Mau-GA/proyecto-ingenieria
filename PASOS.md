# PASOS — Compilar y ejecutar `proyectorestaurante`

Guía para levantar el sistema completo (Streamlit + MariaDB en Docker) en
**Ubuntu/Linux**, desde cero. Ejecuta los bloques en orden.

> Ruta de ejemplo usada en toda la guía: `~/Ing_soft/proyectorestaurante`.
> Si descomprimes en otro lugar, cambia esa ruta.

---

## 0. Qué contiene la carpeta

```
proyectorestaurante/
├── PASOS.md                  ← esta guía
├── README.md                 ← documentación del diseño
├── docker-compose.yml        ← servicios db (MariaDB 11.4) + app (Streamlit)
├── Dockerfile                ← imagen de la app (python:3.11-slim)
├── requirements.txt          ← dependencias Python fijadas
├── .env.example              ← plantilla de variables (copiar a .env)
├── .dockerignore / .gitignore
├── db/
│   ├── schema.sql            ← 33 tablas normalizadas (3FN)
│   └── seed.sql              ← catálogos + datos de prueba
└── app/
    ├── app.py                ← entrada: conexión → login → menú por rol
    ├── database.py           ← SQLAlchemy: engine, run_query, execute, transaccion()
    ├── auth.py               ← login con bcrypt
    ├── permissions.py        ← qué rol ve qué módulo
    ├── set_admin_password.py ← fija la contraseña real de "admin"
    ├── style.css             ← tema oscuro
    ├── .streamlit/config.toml
    ├── components/sidebar.py
    ├── services/ventas.py    ← venta + descuento de inventario por receta (transaccional)
    └── modules/
        ├── pos.py            ← 💳 Punto de venta (pantalla inicial)
        ├── mesas.py, ordenes.py
        ├── productos.py, inventario.py, compras.py
        └── usuarios.py, reportes.py
```

---

## 1. Requisitos (verificar antes de instalar)

```bash
docker --version          # Docker 24 o superior
docker compose version    # plugin Compose v2 ("docker compose", con espacio)
```

Si falta alguno, instálalo (Ubuntu 22.04 / 24.04):

```bash
sudo apt update
sudo apt install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Usar docker sin sudo (cierra sesión y vuelve a entrar después)
sudo usermod -aG docker $USER
```

Prueba: `docker run hello-world`

---

## 2. Descomprimir el proyecto

```bash
mkdir -p ~/Ing_soft
cd ~/Ing_soft
unzip ~/Descargas/proyectorestaurante.zip     # ajusta la ruta del .zip
cd ~/Ing_soft/proyectorestaurante
ls -la                                         # debes ver docker-compose.yml, app/, db/
```

---

## 3. Configurar variables de entorno

```bash
cd ~/Ing_soft/proyectorestaurante
cp .env.example .env

# Genera una SECRET_KEY aleatoria
python3 -c "import secrets; print(secrets.token_hex(32))"

nano .env
```

En `.env` cambia **como mínimo**:

| Variable              | Qué poner                                         |
|-----------------------|---------------------------------------------------|
| `MYSQL_ROOT_PASSWORD` | contraseña larga para root de MariaDB             |
| `MYSQL_PASSWORD`      | contraseña del usuario de la app                  |
| `SECRET_KEY`          | la cadena que generaste arriba                    |

> `MYSQL_HOST` y `MYSQL_PORT` del `.env` **no afectan a Docker**
> (docker-compose fuerza `db:3306`). Solo sirven si corres la app sin Docker (paso 10).

Guarda con `Ctrl+O`, `Enter`, sale con `Ctrl+X`.

---

## 4. Compilar la imagen y levantar los servicios

```bash
cd ~/Ing_soft/proyectorestaurante
docker compose up -d --build
```

Qué pasa:
1. Se construye la imagen `app` (instala `requirements.txt`; la primera vez tarda 1–3 min).
2. Arranca MariaDB. **Solo en el primer arranque** (volumen vacío) ejecuta
   `db/schema.sql` y luego `db/seed.sql`.
3. La app espera a que la BD esté `healthy` y entonces arranca Streamlit.

---

## 5. Verificar

```bash
docker compose ps
```

Esperado (puede tardar ~30 s en pasar a healthy):

```
NAME              STATUS
restaurante_db    Up ... (healthy)
restaurante_app   Up ... (healthy)
```

Si algo no arranca:

```bash
docker compose logs -f db      # errores del SQL de inicialización
docker compose logs -f app     # errores de Python/Streamlit
```

Comprobar que la BD se cargó:

```bash
docker compose exec db sh -c 'mariadb -u"$MARIADB_USER" -p"$MARIADB_PASSWORD" "$MARIADB_DATABASE" -e "SELECT COUNT(*) AS productos FROM productos; SELECT COUNT(*) AS ordenes FROM ordenes;"'
```

Debe mostrar `productos = 10` y `ordenes = 18`.

---

## 6. Fijar la contraseña del administrador (obligatorio)

El seed trae un hash de relleno con el que **no se puede iniciar sesión**:

```bash
docker compose exec app python set_admin_password.py
```

Te pide la contraseña dos veces (mínimo 8 caracteres).

---

## 7. Abrir la aplicación

```
http://localhost:8501
```

Inicia sesión con usuario **`admin`** y la contraseña del paso 6.
La primera pantalla es **💳 Punto de venta**.

---

## 8. Prueba rápida del descuento de inventario

1. **Inventario** → anota el stock de *Carne Res* (50.000 kg en el seed).
2. **Punto de venta** → agrega 2× *Hamburguesa Clasica* y 1× *Hamburguesa Doble*.
   Tipo de orden: *Para Llevar*. Efectivo recibido: 500. **Cobrar**.
3. El mensaje muestra la orden, el total ($395.00) y el cambio; abre
   “Insumos descontados del inventario”: Carne Res −0.800 kg (0.2×2 + 0.4).
4. **Inventario** → Carne Res ahora en 49.200 kg.
5. Prueba de stock insuficiente: la *Salsa Buffalo* tiene 1.000 l y cada
   *Alitas Buffalo* usa 0.050 l. Intenta cobrar 21 alitas: la venta se
   rechaza indicando el insumo faltante y **no se guarda nada**.

Cómo funciona (en `app/services/ventas.py`), todo en **una transacción**:

```
ordenes → detalles_orden → consumo por receta (suma por insumo)
        → SELECT ... FOR UPDATE sobre los insumos (evita carreras entre cajas)
        → ¿alcanza? no → ROLLBACK + mensaje con el faltante
                    sí → movimientos_inventario ('Salida por venta', id_orden)
                         + UPDATE insumos.stock_actual
        → total recalculado desde detalles_orden.subtotal → pagos
```

El módulo **Mesas → Órdenes** (cuentas abiertas) usa el mismo servicio.

---

## 9. Operación diaria

```bash
cd ~/Ing_soft/proyectorestaurante

docker compose up -d                 # encender
docker compose down                  # apagar (los datos se conservan)
docker compose restart app           # reiniciar solo la app
docker compose logs -f app           # ver logs en vivo
docker compose up -d --build app     # recompilar tras cambiar requirements.txt
```

- Cambios en archivos `.py` de `app/` **no requieren recompilar**: el código
  está montado como volumen; recarga el navegador.
- Cambios en `requirements.txt` o `Dockerfile` → `docker compose up -d --build`.

**Respaldo y restauración de la BD:**

```bash
mkdir -p ~/Ing_soft/respaldos
docker compose exec -T db sh -c 'mariadb-dump -u"$MARIADB_USER" -p"$MARIADB_PASSWORD" "$MARIADB_DATABASE"' \
  > ~/Ing_soft/respaldos/restaurante_$(date +%F).sql

docker compose exec -T db sh -c 'mariadb -u"$MARIADB_USER" -p"$MARIADB_PASSWORD" "$MARIADB_DATABASE"' \
  < ~/Ing_soft/respaldos/restaurante_AAAA-MM-DD.sql
```

**Reiniciar la BD desde cero** (re-ejecuta schema.sql y seed.sql; **borra todos los datos**):

```bash
docker compose down -v
docker compose up -d --build
docker compose exec app python set_admin_password.py
```

---

## 10. (Opcional) Ejecutar la app sin Docker, contra la BD del contenedor

Útil para depurar con tu editor. Requiere Python 3.11–3.13.

```bash
cd ~/Ing_soft/proyectorestaurante
docker compose up -d db                       # solo la base de datos
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd app
streamlit run app.py                          # usa MYSQL_HOST=127.0.0.1 y MYSQL_PORT=3307 del .env
```

---

## 11. Conectarse con DBeaver

Nueva conexión → **MariaDB**:

| Campo        | Valor                                   |
|--------------|-----------------------------------------|
| Host         | `127.0.0.1`                             |
| Puerto       | `3307` (o tu `MYSQL_EXPOSED_PORT`)      |
| Base de datos| `proyectorestaurante`                   |
| Usuario/Pass | `MYSQL_USER` / `MYSQL_PASSWORD` de `.env` |

El puerto solo escucha en `127.0.0.1` (no queda expuesto a la red local).

---

## 12. Problemas comunes

| Síntoma | Causa / solución |
|---|---|
| `Define MYSQL_ROOT_PASSWORD en .env` al hacer `up` | No existe `.env` o le falta la variable → paso 3. |
| `port is already allocated` (3307 u 8501) | Otro servicio usa el puerto. Cambia `MYSQL_EXPOSED_PORT` o `APP_PORT` en `.env`. |
| La app dice “No hay conexión con la base de datos” | La BD aún inicializa; espera 30 s y recarga. Si persiste: `docker compose logs db`. |
| Cambié `schema.sql`/`seed.sql` y no se refleja | Solo corren con el volumen vacío → `docker compose down -v` (borra datos). |
| “Usuario o contraseña incorrectos” con `admin` | Falta el paso 6. |
| “Tu usuario no está vinculado a una ficha de empleado” | Los movimientos de inventario exigen empleado. Crea el usuario en **Usuarios** vinculándolo a un empleado. |
| “No se registró la venta: no alcanza el inventario” | Registra una **Compra** o un **Ajuste positivo** del insumo indicado. |
| Al abrir una mesa en **Órdenes** aparece una cuenta con importe previo | Las órdenes históricas del seed están en estado *Entregada* (no final), así que cuentan como abiertas. Para cerrarlas: `UPDATE ordenes SET id_estado = 5 WHERE id_orden <= 18 AND id_estado = 4;` |
| `permission denied ... docker.sock` | Falta `sudo usermod -aG docker $USER` y volver a iniciar sesión. |
