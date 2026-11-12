# AGENTS.md

Contexto persistente del proyecto para futuras sesiones de trabajo.

## Descripción

Aplicación de restaurante (POS, mesas, órdenes, KDS, inventario, compras,
usuarios y reportes) con:

- **UI:** Streamlit (`app/app.py`)
- **Datos:** PostgreSQL 16 + SQLAlchemy 2.x + psycopg2 (`app/database.py`)
- **Auth:** bcrypt + sesión de Streamlit (`app/auth.py`)
- **Autorización:** RBAC por roles (`app/permissions.py`)

Todo el código, comentarios y documentación están en español.

## Estructura

```
app/
  app.py                 # entrada Streamlit, login y despachador de módulos
  database.py            # Engine cacheado, run_query/execute/transaccion
  auth.py                # login/logout, verificación bcrypt, carga de roles
  permissions.py         # roles, módulos y acciones permitidas
  components/sidebar.py  # navegación filtrada por rol
  components/merma.py    # popover compartido para reportar mermas
  modules/               # foodhouse, mesas, ordenes, comandas, mermas_reporte,
                         # productos, inventario, compras, usuarios, reportes
  services/ventas.py     # lógica de ventas
  services/mermas.py     # mermas de producto/partida -> movimientos + mermas
  set_admin_password.py  # regenera el hash bcrypt del usuario admin
  crear_usuarios_prueba.py # crea/actualiza cuentas de prueba por rol
  actualizar_unidades_inventario.py # migra alitas/refrescos a piezas
db/
  schema.sql             # DDL normalizado (3FN)
  seed.sql               # catálogos, empleados y datos históricos
docker-compose.yml       # servicios db y app
Dockerfile
requirements.txt
```

## Comandos

```bash
# Levantar todo (primer arranque ejecuta db/schema.sql y db/seed.sql)
docker compose up -d --build

# Ver estado / logs
docker compose ps
docker compose logs -f app

# Ejecutar utilidades dentro del contenedor
docker compose exec app python set_admin_password.py
docker compose exec app python crear_usuarios_prueba.py
docker compose exec app python actualizar_unidades_inventario.py

# Re-inicializar la BD desde cero (BORRA DATOS)
docker compose down -v && docker compose up -d

# Verificar sintaxis sin dependencias instaladas en el host
python3 -m py_compile app/*.py app/modules/*.py app/components/*.py app/services/*.py
```

No hay suite de tests automatizada. La app se monta con bind mount (`./app:/app`),
por lo que Streamlit recarga al guardar cambios.

## Base de datos y reglas clave

- Roles sembrados: `Administrador`, `Gerente`, `Mesero`, `Cocina`, `Bar`, `Caja`,
  `Inventario`, `Limpieza`. El rol `Inventario` quedó retirado de la app y sus
  funciones las absorbe el Gerente.
- `usuarios.id_empleado` es **NULL permitido** y **UNIQUE**: una ficha de empleado
  solo puede pertenecer a un usuario. El `admin` del seed ya ocupa el empleado 7.
- `password_hash` debe ser bcrypt (nunca texto plano). El hash del seed para
  `admin` es un placeholder inválido hasta ejecutar `set_admin_password.py`.
- `usuarios_roles` es N:M; un usuario sin roles activos no puede iniciar sesión.
- La app usa la API de `database.py`; no abrir conexiones SQLAlchemy por fuera.
- SQL siempre con parámetros nombrados (`:param`), nunca f-strings con datos.
- Los scripts de `db/` solo se ejecutan al crear el volumen por primera vez.
- FoodHouse "Mandar comanda" crea la orden en `Pendiente` sin pago, con todas
  sus partidas en `Pendiente`; se cobra después en Órdenes > Delivery.
- `agregar_partidas` expande cada unidad a una fila con `cantidad = 1` para que
  Comandas/Mermas puedan actuar sobre una sola pieza.
- El cobro se bloquea hasta que todas las partidas no mermadas estén en
  `Listo` o `Entregado` (validado en UI y en `cobrar_orden_mesa`). No existe un
  estado `Mermado` en el esquema: la merma se marca en `auditoria` y esas
  partidas se excluyen del cobro.
- Si todas las partidas activas de una orden quedan mermadas,
  `registrar_merma_detalle` cancela la orden y libera su mesa (si tiene),
  dejándola en `Disponible`. La excepción es "Mermar y repetir", que conserva
  la mesa ocupada porque inserta una reposición en la misma orden.
- Las mesas solo admiten los estados `Disponible` y `Ocupada`.
- En Comandas > Listo, el Gerente puede pulsar "Limpiar sección" para pasar
  todas las partidas `Listo` a `Entregado`; la acción `comandas.limpiar` es
  exclusiva del rol Gerente.
- El número visible de orden se guarda en `ordenes.numero_orden_diaria` y se
  asigna al crear la orden con `MAX(...) + 1` del `CURRENT_DATE`; reinicia a 1
  cada día. El `id_orden` queda solo para relaciones internas.
- Las mermas se reportan sin tocar stock. "Subir merma" en Inventario descuenta
  formalmente el inventario y fija `costo_estimado_perdida`.
- El formulario de Mermas usa un selector combinado `[Menú]` / `[Insumo]` con
  todos los productos e insumos activos. Producto preparado ->
  `registrar_merma_producto` (un movimiento por insumo de receta; requiere
  receta); insumo base -> `registrar_merma_insumo` (movimiento directo). El
  Gerente distingue el tipo por el prefijo del motivo (`Merma producto` vs
  `Merma insumo`) y la bandeja muestra "Producto preparado" o "Insumo base".
- Inventario tiene una pestaña `Stock Crítico` que lista solo insumos activos
  con `stock_actual <= stock_minimo`, ordenados por faltante.
- La pestaña `Insumos` permite alta/edición de insumos. Los nombres que
  contengan alita, coca, refresco o soda se guardan siempre con unidad
  `Pieza (pza)`; el stock actual solo se modifica desde `Registrar ajuste`.
- `Alitas Pollo` y `Refresco Cola` se manejan por pieza. La conversión usada
  es 1 kg = 20 alitas y 1 refresco = 0.6 l. El script
  `actualizar_unidades_inventario.py` es idempotente y ajusta stock, mínimos,
  costos, recetas, movimientos y compras históricas.

## Cuentas de prueba

`app/crear_usuarios_prueba.py` es idempotente y genera/actualiza:

| Usuario       | Rol        |
|---------------|------------|
| `gerente1`    | Gerente    |
| `mesero1`     | Mesero     |
| `cajero1`     | Caja       |
| `cocina1`     | Cocina     |
| `bar1`        | Bar        |

Las contraseñas están definidas en la tupla `CUENTAS` de ese archivo. El script
hashea con bcrypt, vincula cada cuenta a un empleado activo libre (prioriza el
puesto del rol) y muestra una tabla resumen al final.

## Convenciones

- Mantener el estilo existente: docstrings y comentarios en español, SQL
  parametrizado, funciones pequeñas y validación de rol al inicio de cada módulo.
- Para acciones sensibles usar `permissions.requerir_rol` / `requerir_accion`
  además de la validación previa ya hecha en cada `render()`.
- No commitear `.env` ni secretos. Las credenciales reales viven en `.env`.
- Al terminar cambios: ejecutar `py_compile` sobre los archivos tocados y, si los
  contenedores están activos, probar el flujo afectado con Docker.
