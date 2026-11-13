# Los Bro's Restaurant — Sistema web de administración

Proyecto para el equipo **Axon Analytics**: convierte la base de datos del
restaurante en un sistema web funcional con Streamlit + PostgreSQL + Docker.

> **¿Solo quieres levantarlo?** Sigue [`PASOS.md`](PASOS.md): instalación,
> compilación con `docker compose up -d --build`, verificación y solución de
> problemas.

---

## 0. Cambios de esta versión

- **Nuevo módulo 💳 Punto de venta** (`app/modules/pos.py`), pantalla inicial
  para Administrador, Gerente, Mesero y Caja: catálogo por categoría con
  búsqueda, carrito con +/−, tipos de orden (Local con mesa, Para llevar,
  Domicilio, Plataforma externa con número de pedido), métodos de pago con
  referencia y cálculo de cambio en efectivo.
- **Descuento de inventario por receta transaccional**
  (`app/services/ventas.py`): orden, partidas, salida de insumos, total y
  pago se guardan en **una sola transacción**. Se valida el stock sumando el
  consumo por insumo de toda la venta, con bloqueo `SELECT … FOR UPDATE`; si
  algo no alcanza, se hace ROLLBACK y se informa qué insumo falta.
- **Órdenes por mesa** (`app/modules/ordenes.py`) usa el mismo servicio:
  guardar y cobrar ahora son atómicos (antes eran sentencias sueltas).
- `database.py` lee las variables de entorno directamente (se retiró
  `config.py`), usa `URL.create` (contraseñas con caracteres especiales) y
  agrega `transaccion()` y `verificar_conexion()`.
- `auth.py`: corregido el caso de usuario sin empleado (pandas entrega `NaN`,
  no `None`, para un `id_empleado` nulo).
- Docker: healthcheck con Python (la imagen slim no trae `curl`), PostgreSQL
  16 con UTF8/citext, puerto de BD solo en `127.0.0.1`, validación de
  variables obligatorias del `.env`.

---

## 1. Análisis de lo proporcionado

### 1.1 Base de datos (`schema.sql` + `seed.sql`)

Los archivos **ya son un rediseño normalizado a 3FN** de un dump original
(`proyectorestaurante.sql`):

- **Motor:** PostgreSQL 16. Usa columnas `IDENTITY`, `CITEXT`, columnas
  generadas (`GENERATED ALWAYS AS (...) STORED`) y `JSONB` para auditoría.
- **Catálogos en vez de VARCHAR libres:** roles, permisos, puestos,
  unidades de medida, métodos de pago, estados de orden/detalle, tipos de
  orden, plataformas externas, tipos de movimiento de inventario. Esto evita
  errores de captura ("Efectivo" vs "efectivo" vs "EFVO").
- **Separación usuarios/empleados:** un empleado puede no tener cuenta de
  acceso, y una cuenta administrativa puede no tener ficha laboral. Lo
  respeta el código (ver sección 10).
- **Mermas en dos fases:** al reportar se crea la fila en `mermas` con
  `id_movimiento = NULL` y una marca JSON en `auditoria`; el stock no cambia.
  Al subirla, una partida ya vendida solo registra el costo (la venta ya
  descontó receta) y una captura manual crea el movimiento `Merma` y descuenta
  stock en ese momento.
- **Seed consistente:** las órdenes históricas quedan `Pagada` o `Cancelada`,
  sus partidas `Entregado`, los totales coinciden con la suma de partidas y
  pagos, y todas las mesas inician `Disponible`. La orden 18, sin partidas,
  queda cancelada con total 0.

### 1.2 ¿Por qué PostgreSQL?

**PostgreSQL 16.** El schema usa `IDENTITY`, `CITEXT`, columnas generadas,
`JSONB`, índices sobre expresiones e integridad referencial estricta. La app se
conecta con SQLAlchemy + `psycopg2`; los scripts de `db/` se cargan con `psql`.

### 1.3 Interfaz de referencia ("POSFood")

Punto de venta oscuro con pestañas de categoría, tarjetas de producto con
precio y panel de orden a la derecha. Se adaptó la intención visual (tema
oscuro, acento rojo/naranja, tarjetas redondeadas) con `st.columns`,
`st.tabs` y CSS propio (`app/style.css`). No hay fotos de producto porque
`productos` no tiene campo de imagen; si se requieren, agregar `url_imagen`.

---

## 2. Arquitectura

```
Navegador ──▶ Streamlit (contenedor "app")  ──▶ PostgreSQL (contenedor "db")
                     ▲                                 ▲
                     │                                 │
              docker-compose.yml conecta ambos     DBeaver (desde tu
              por la red interna restaurante_net    máquina, 127.0.0.1:3307)
```

Capas dentro de `app/`:

```
app.py ─▶ components/sidebar.py ─▶ modules/*.py (interfaz por módulo)
                                        │
                                        ▼
                               services/ventas.py (reglas de negocio, transacciones)
                                        │
                                        ▼
                                  database.py (SQLAlchemy + psycopg2)
```

- **SQLAlchemy + psycopg2**, siempre con parámetros nombrados (nunca
  f-strings en SQL) para evitar inyección SQL.
- **bcrypt** para contraseñas.
- **Docker Compose** con dos servicios en una red interna; el código de la
  app se monta como volumen para desarrollo ágil.

---

## 3. Estructura de carpetas

```
proyectorestaurante/
├── PASOS.md                    Guía de compilación y ejecución
├── README.md                   (este archivo)
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
├── .dockerignore
├── .gitignore
├── db/
│   ├── schema.sql              Schema normalizado (sin cambios)
│   └── seed.sql                Datos de prueba (sin cambios)
└── app/
    ├── app.py                  Entrada: conexión + login + navegación
    ├── database.py             Engine SQLAlchemy + helpers + transaccion()
    ├── auth.py                 Login, logout, sesión
    ├── permissions.py          Qué rol ve/hace qué
    ├── style.css               Tema oscuro
    ├── set_admin_password.py   Utilidad para fijar la clave de admin
    ├── .streamlit/config.toml  Tema oscuro nativo de Streamlit
    ├── services/
    │   └── ventas.py           Venta, descuento de inventario, cobro (transaccional)
    ├── modules/
    │   ├── pos.py              Punto de venta (mostrador)
    │   ├── mesas.py            Mapa de mesas
    │   ├── ordenes.py          Cuentas abiertas por mesa y cobro
    │   ├── productos.py        Catálogo del menú
    │   ├── inventario.py       Existencias y ajustes
    │   ├── compras.py          Alta de compras a proveedor
    │   ├── usuarios.py         Alta de usuarios y roles (admin)
    │   └── reportes.py         Ventas, top productos, stock bajo
    └── components/
        └── sidebar.py          Navegación filtrada por rol
```

---

## 4–9. Instalación, Docker, DBeaver, ejecución

Todo el procedimiento paso a paso está en **[`PASOS.md`](PASOS.md)**.

Resumen:

```bash
cd ~/Ing_soft/proyectorestaurante
cp .env.example .env && nano .env
docker compose up -d --build
docker compose exec app python set_admin_password.py
# http://localhost:8501  (usuario: admin)
```

### Host/puerto según dónde te conectes

| Desde...                      | Host        | Puerto                                 |
|-------------------------------|-------------|----------------------------------------|
| Streamlit (dentro de Docker)  | `db`        | `5432`                                 |
| DBeaver / app sin Docker      | `127.0.0.1` | `MYSQL_EXPOSED_PORT` (por defecto 3307)|

### Git y GitHub

```bash
cd ~/Ing_soft/proyectorestaurante
git init
git checkout -b main
git add .
git commit -m "Estructura inicial: Docker, Streamlit, BD, POS con inventario"
git remote add origin https://github.com/<tu_usuario>/proyectorestaurante.git
git push -u origin main
```

Flujo de equipo: una rama por funcionalidad (`feature/...`) y Pull Request
hacia `main`. **Nunca subir `.env`** (ya está en `.gitignore`).

---

## 10. Autenticación, roles y permisos

- **Login** (`auth.py`): valida usuario/contraseña con `bcrypt` y carga los
  roles desde `usuarios_roles` + `roles`.
- **Permisos** (`permissions.py`): `MODULOS` define qué roles ven cada
  módulo; el primero visible es la pantalla inicial. Además, **cada módulo
  llama a `requerir_rol(...)`** antes de leer o escribir datos.
- Roles del seed: `Administrador`, `Gerente`, `Mesero`, `Cocina`, `Bar`,
  `Caja`, `Inventario` (más `Limpieza`, solo de referencia).
- Los movimientos de inventario exigen `id_empleado` (columna NOT NULL). Si
  el usuario no está vinculado a un empleado, el POS solo permite vender
  productos sin receta y muestra un aviso claro.

| Módulo          | Roles                                          |
|-----------------|------------------------------------------------|
| Punto de venta  | Administrador, Gerente, Mesero, Caja           |
| Mesas           | Administrador, Gerente, Mesero, Caja           |
| Órdenes         | Administrador, Gerente, Mesero, Cocina, Bar, Caja |
| Productos       | Administrador, Gerente                         |
| Inventario      | Administrador, Gerente, Inventario             |
| Compras         | Administrador, Gerente, Inventario             |
| Usuarios        | Administrador                                  |
| Reportes        | Administrador, Gerente                         |

---

## 11. Módulos

- **Punto de venta:** venta de mostrador cobrada al momento. Crea la orden
  en estado *Pagada* con sus partidas en *Pendiente* (para que cocina/bar
  las preparen), descuenta inventario por receta y registra el pago, todo
  en una transacción. Muestra el detalle de insumos descontados y las
  ventas del día.
- **Mesas:** tarjetas con número, capacidad y estado de cada mesa. “Abrir
  orden” lleva a Órdenes con esa mesa seleccionada.
- **Órdenes:** cuenta abierta por mesa; al guardar crea la orden (o
  reutiliza la abierta), inserta partidas, **descuenta inventario** y marca
  la mesa *Ocupada*. “Cobrar y cerrar mesa” registra el pago y libera la mesa
  solo si no quedan otras cuentas abiertas en ella.
- **Productos:** catálogo con categoría, área de preparación y precio.
- **Inventario:** existencias con alerta de stock mínimo, ajustes manuales
  atómicos y bandeja de mermas pendientes. “Subir merma” confirma el costo;
  solo las capturas manuales descuentan stock (las partidas vendidas ya lo
  hicieron al cobrar).
- **Compras:** compras a proveedor; cada partida suma al stock.
- **Usuarios** (solo Administrador): usuarios, roles y empleados.
- **Reportes:** total cobrado, desglose por método de pago, productos más
  vendidos e insumos con stock bajo.

### Regla de inventario (services/ventas.py)

1. Consumo por insumo = Σ (cantidad vendida × `recetas.cantidad_usada`) de
   todas las partidas (si dos productos usan el mismo insumo, se valida la suma).
2. `SELECT … FOR UPDATE` sobre esos insumos (dos cajas no pueden dejar el
   stock en negativo).
3. Si falta stock → `StockInsuficienteError` y ROLLBACK de toda la venta.
   `insumos.stock_actual` además tiene `CHECK (stock_actual >= 0)`.
4. Un movimiento *Salida por venta* por insumo (con `id_orden`) y
   `UPDATE insumos.stock_actual`.
5. Productos sin receta se venden sin descontar nada.

---

## 12. Guía de pruebas

Pruebas automatizadas (crean y recargan una base `*_test`):

```bash
docker compose run --rm -v "$PWD/tests:/app/tests" -v "$PWD/db:/app/db" app pytest -q /app/tests
```

Pruebas manuales:

1. Levanta el proyecto y fija la contraseña de `admin` (PASOS 4–6).
2. Inicia sesión: debes ver los 8 módulos, empezando por Punto de venta.
3. **POS:** vende 2 Hamburguesa Clasica + 1 Hamburguesa Doble (Para llevar,
   efectivo 500). Debe mostrar total $395.00, cambio $105.00 y Carne Res −0.800 kg.
4. **Inventario:** confirma que Carne Res bajó de 50.000 a 49.200.
5. **Stock insuficiente:** intenta vender 21 Alitas Buffalo (Salsa Buffalo
   1.000 l, 0.050 l c/u): debe rechazarse sin guardar nada.
6. **Usuarios → Nuevo usuario:** crea uno con rol `Mesero`. Entra con él:
   solo debe ver Punto de venta, Mesas y Órdenes.
7. **Mesas → Abrir orden → Órdenes:** agrega productos y guarda; la mesa
   pasa a *Ocupada*. Cobra: vuelve a *Disponible*.
8. **Compras:** registra una compra y confirma que el stock sube.
9. **Reportes:** las ventas de hoy aparecen en el rango de fechas actual.
