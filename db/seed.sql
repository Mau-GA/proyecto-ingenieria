-- =====================================================================
-- proyectorestaurante — SEED DATA (PostgreSQL 16)
-- Contiene: (A) catálogos base necesarios para operar el sistema y
--           (B) los datos del dump original (`proyectorestaurante.sql`)
--           migrados al nuevo modelo.
--
-- REGLA SEGUIDA: los datos del dump se conservan siempre que fue posible.
-- Ninguna inconsistencia encontrada se corrigió en silencio: cada caso
-- se documenta con un comentario junto al INSERT afectado y se explica
-- con más detalle en MIGRACION.md.
-- =====================================================================

-- ---------------------------------------------------------------------
-- A.1 Roles y permisos (catálogo de arranque; ajustar a la operación real)
-- ---------------------------------------------------------------------
INSERT INTO roles (id_rol, nombre, descripcion) VALUES
(1, 'Administrador', 'Acceso total al sistema'),
(2, 'Gerente', 'Gestión operativa del restaurante'),
(3, 'Mesero', 'Captura y atiende órdenes en mesa'),
(4, 'Cocina', 'Visualiza y actualiza comandas de cocina'),
(5, 'Bar', 'Visualiza y actualiza comandas de bar'),
(6, 'Caja', 'Registra pagos y emite tickets'),
(7, 'Inventario', 'Gestiona insumos, compras y movimientos de stock'),
(8, 'Limpieza', 'Sin acceso al sistema (rol de referencia, no operativo)');

INSERT INTO permisos (id_permiso, clave, descripcion) VALUES
(1, 'usuarios.gestionar', 'Crear/editar usuarios y asignar roles'),
(2, 'ordenes.crear', 'Capturar nuevas órdenes'),
(3, 'ordenes.editar', 'Modificar órdenes existentes'),
(4, 'ordenes.cancelar', 'Cancelar órdenes o partidas'),
(5, 'pagos.registrar', 'Registrar pagos y emitir tickets'),
(6, 'inventario.gestionar', 'Ajustar stock y registrar movimientos'),
(7, 'compras.gestionar', 'Registrar compras a proveedores'),
(8, 'reportes.ver', 'Consultar reportes y auditoría'),
(9, 'catalogos.gestionar', 'Administrar productos, insumos y catálogos');

-- Ejemplo de asignación inicial: ajustar según las políticas reales del negocio.
INSERT INTO roles_permisos (id_rol, id_permiso)
SELECT 1, id_permiso FROM permisos; -- Administrador obtiene todos los permisos

-- ---------------------------------------------------------------------
-- A.2 Catálogos generales
-- ---------------------------------------------------------------------
INSERT INTO puestos (id_puesto, nombre) VALUES
(1, 'Mesero'), (2, 'Cocinero'), (3, 'Bartender'),
(4, 'Caja'), (5, 'Gerente'), (6, 'Inventario'), (7, 'Limpieza');

INSERT INTO unidades_medida (id_unidad, nombre, abreviatura, tipo_magnitud) VALUES
(1, 'Kilogramo', 'kg', 'Masa'),
(2, 'Litro', 'l', 'Volumen'),
(3, 'Pieza', 'pza', 'Pieza');

INSERT INTO areas_preparacion (id_area, nombre) VALUES
(1, 'Cocina'), (2, 'Bar');

INSERT INTO metodos_pago (id_metodo_pago, nombre, requiere_referencia) VALUES
(1, 'Efectivo', FALSE),
(2, 'Tarjeta', TRUE),
(3, 'Transferencia', TRUE),
(4, 'Plataforma Externa', TRUE);

INSERT INTO estados_orden (id_estado, nombre, orden_flujo, es_final) VALUES
(1, 'Pendiente', 1, FALSE),
(2, 'En preparación', 2, FALSE),
(3, 'Lista', 3, FALSE),
(4, 'Entregada', 4, FALSE),
(5, 'Pagada', 5, TRUE),
(6, 'Cancelada', 6, TRUE);

INSERT INTO estados_detalle_orden (id_estado, nombre, es_final) VALUES
(1, 'Pendiente', FALSE),
(2, 'En preparación', FALSE),
(3, 'Listo', FALSE),
(4, 'Entregado', TRUE),
(5, 'Cancelado', TRUE);

INSERT INTO tipos_orden (id_tipo_orden, nombre, requiere_mesa, requiere_plataforma) VALUES
(1, 'Local', TRUE, FALSE),
(2, 'Para Llevar', FALSE, FALSE),
(3, 'Domicilio Propio', FALSE, FALSE),
(4, 'Plataforma Externa', FALSE, TRUE);

-- Catálogo preparado para integraciones futuras (sin datos históricos, el dump
-- original no registraba pedidos de plataformas externas).
INSERT INTO plataformas_externas (id_plataforma, nombre, comision_porcentaje) VALUES
(1, 'Rappi', NULL),
(2, 'Uber Eats', NULL),
(3, 'DiDi Food', NULL);

-- Se separa 'Merma/Ajuste' del catálogo original en dos conceptos: 'Merma'
-- (siempre debe acompañarse de una fila en la tabla `mermas`) y 'Ajuste
-- positivo'/'Ajuste negativo' (correcciones de conteo físico que NO son una
-- pérdida y por tanto no requieren fila en `mermas`).
INSERT INTO tipos_movimiento_inventario (id_tipo_movimiento, nombre, efecto) VALUES
(1, 'Entrada por compra', 'Entrada'),
(2, 'Salida por venta', 'Salida'),
(3, 'Merma', 'Salida'),
(4, 'Ajuste positivo', 'Entrada'),
(5, 'Ajuste negativo', 'Salida');

INSERT INTO tipos_merma (id_tipo_merma, nombre, requiere_reporte_autoridad, descripcion) VALUES
(1, 'Caducidad', FALSE, 'Insumo venció antes de poder usarse'),
(2, 'Merma de cocina', FALSE, 'Error de preparación, corte o manejo en cocina/bar'),
(3, 'Accidente', FALSE, 'Caída, derrame u otro accidente operativo'),
(4, 'Robo', TRUE, 'Sustracción de insumos o producto'),
(5, 'Otro', FALSE, 'Cualquier causa no cubierta por los tipos anteriores');

INSERT INTO categorias_insumos (id_categoria, nombre, descripcion) VALUES
(1, 'Carnes', 'Productos carnicos'),
(2, 'Verduras', 'Vegetales frescos'),
(3, 'Panaderia', 'Panes y harinas'),
(4, 'Salsas', 'Salsas y aderezos'),
(5, 'Bebidas', 'Ingredientes bebidas'),
(6, 'Lacteos', 'Productos lacteos');

INSERT INTO categorias_productos (id_categoria, nombre, descripcion) VALUES
(1, 'Hamburguesas', 'Hamburguesas del menu'),
(2, 'Alitas', 'Alitas con diferentes salsas'),
(3, 'Bebidas', 'Bebidas frias y calientes');

-- ---------------------------------------------------------------------
-- B.1 Empleados y usuarios
-- ---------------------------------------------------------------------
INSERT INTO empleados (id_empleado, nombre, apellido, telefono, id_puesto, salario, fecha_ingreso, estatus) VALUES
(1, 'Pedro', 'Gomez',    '5511111111', 1, 7500.00, '2024-01-10', 'Activo'),
(2, 'Laura', 'Diaz',     '5522222222', 1, 7600.00, '2024-02-15', 'Activo'),
(3, 'Mario', 'Ruiz',     '5533333333', 2, 9500.00, '2023-11-01', 'Activo'),
(4, 'Sofia', 'Mendez',   '5544444444', 2, 9800.00, '2023-12-05', 'Activo'),
(5, 'Diego', 'Castro',   '5555555555', 3, 8500.00, '2024-03-20', 'Activo'),
(6, 'Elena', 'Vargas',   '5566666666', 4, 8000.00, '2024-01-25', 'Activo'),
(7, 'Raul',  'Morales',  '5577777777', 5, 15000.00,'2022-05-12', 'Activo'),
(8, 'Paula', 'Jimenez',  '5588888888', 6, 9000.00, '2023-09-18', 'Activo'),
(9, 'Jorge', 'Navarro',  '5599999999', 7, 6500.00, '2024-04-10', 'Activo');

-- El dump original no manejaba cuentas de acceso. Se crea únicamente una
-- cuenta administrativa "bootstrap" para poder operar el sistema desde el
-- día uno. El hash es un PLACEHOLDER: debe regenerarse con
-- set_admin_password.py y cambiarse antes de usar el sistema en producción.
INSERT INTO usuarios (id_usuario, id_empleado, nombre_usuario, correo, password_hash, activo) VALUES
(1, 7, 'admin', NULL, '$2y$10$CAMBIAR.ESTE.HASH.EN.PRODUCCION.0000000000000000000000', TRUE);

INSERT INTO usuarios_roles (id_usuario, id_rol) VALUES (1, 1);

-- ---------------------------------------------------------------------
-- B.2 Clientes y proveedores
-- ---------------------------------------------------------------------
INSERT INTO clientes (id_cliente, nombre, telefono) VALUES
(1, 'Juan Perez',     '5512345678'),
(2, 'Maria Lopez',    '5587654321'),
(3, 'Carlos Ramirez', '5544332211'),
(4, 'Ana Torres',     '5511223344'),
(5, 'Luis Hernandez', '5599887766');

INSERT INTO proveedores (id_proveedor, nombre, telefono, direccion, correo) VALUES
(1, 'Carnes del Norte',     '5510101010', 'CDMX',   'ventas@carnesnorte.com'),
(2, 'Distribuidora Fresh',  '5520202020', 'Toluca', 'contacto@fresh.com'),
(3, 'Bebidas Express',      '5530303030', 'Puebla', 'ventas@bebidasexpress.com');

-- ---------------------------------------------------------------------
-- B.3 Insumos, productos y recetas
-- ---------------------------------------------------------------------
-- Alitas Pollo y Refresco Cola se administran por pieza. Las cantidades
-- históricas se convirtieron con 1 kg = 20 alitas y 1 refresco = 0.6 l.
INSERT INTO insumos (id_insumo, nombre, descripcion, id_categoria, id_unidad_medida, stock_actual, stock_minimo, costo_unitario_referencia) VALUES
(1,  'Carne Res',            'Carne para hamburguesa', 1, 1, 50.00, 10.00, 180.00),
(2,  'Pan Hamburguesa',      'Pan artesanal',          3, 3, 100.00,20.00,   8.00),
(3,  'Queso Cheddar',        'Queso amarillo',         6, 1, 20.00, 5.00, 150.00),
(4,  'Lechuga',               'Lechuga fresca',        2, 1, 15.00, 3.00,  40.00),
(5,  'Tomate',                'Tomate rojo',           2, 1, 20.00, 5.00,  35.00),
(6,  'Alitas Pollo',          'Alitas frescas por pieza', 1, 3, 1200.00,300.00, 6.00),
(7,  'Salsa BBQ',             'Salsa BBQ dulce',       4, 2, 10.00, 2.00,  90.00),
(8,  'Salsa Buffalo',         'Salsa picante',         4, 2,  1.00, 2.00,  95.00),
(9,  'Salsa Mango Habanero',  'Salsa especial',        4, 2,  6.00, 2.00, 110.00),
(10, 'Refresco Cola',         'Refresco envasado por pieza', 5, 3, 50.00, 9.00, 15.00),
(11, 'Limones',               'Limones frescos',       2, 1, 15.00, 3.00,  30.00),
(12, 'Chocolate',             'Chocolate para malteada',5, 1,  8.00, 2.00, 140.00),
(13, 'Cafe Molido',           'Cafe premium',          5, 1,  5.00, 1.00, 220.00),
(14, 'Leche',                 'Leche entera',          6, 2, 20.00, 5.00,  28.00);

-- id_area_preparacion se infiere de la categoría de producto usando el mismo
-- criterio observado de forma consistente en `detalles_orden` del dump
-- original (categorías 1 y 2 -> Cocina; categoría 3 -> Bar).
INSERT INTO productos (id_producto, nombre, descripcion, id_categoria, id_area_preparacion, precio_actual, disponible) VALUES
(1,  'Hamburguesa Clasica',    'Carne, queso y verduras',   1, 1, 120.00, TRUE),
(2,  'Hamburguesa BBQ',        'Hamburguesa con salsa BBQ', 1, 1, 135.00, TRUE),
(3,  'Hamburguesa Doble',      'Doble carne y queso',       1, 1, 155.00, TRUE),
(4,  'Alitas BBQ',             'Alitas con salsa BBQ',      2, 1, 140.00, TRUE),
(5,  'Alitas Buffalo',         'Alitas picantes',           2, 1, 145.00, TRUE),
(6,  'Alitas Mango Habanero',  'Alitas dulces picantes',    2, 1, 150.00, TRUE),
(7,  'Coca Cola',              'Refresco 600ml',            3, 2,  35.00, TRUE),
(8,  'Limonada',               'Limonada natural',          3, 2,  45.00, TRUE),
(9,  'Malteada Chocolate',     'Malteada cremosa',          3, 2,  70.00, TRUE),
(10, 'Cafe Americano',         'Cafe negro',                3, 2,  40.00, TRUE);

INSERT INTO recetas (id_producto, id_insumo, cantidad_usada) VALUES
(1, 1, 0.20), (1, 2, 1.00), (1, 3, 0.05), (1, 4, 0.03), (1, 5, 0.04),
(2, 1, 0.20), (2, 2, 1.00), (2, 3, 0.05), (2, 7, 0.03),
(3, 1, 0.40), (3, 2, 1.00), (3, 3, 0.08),
(4, 6, 10.00), (4, 7, 0.05),
(5, 6, 10.00), (5, 8, 0.05),
(6, 6, 10.00), (6, 9, 0.05),
(7, 10, 1.00),
(8, 11, 0.20),
(9, 12, 0.10), (9, 14, 0.30),
(10, 13, 0.03);

-- ---------------------------------------------------------------------
-- B.4 Compras y detalle de compra
-- ---------------------------------------------------------------------
-- ADVERTENCIA DE MIGRACIÓN: en el dump original, `compras.total` NO coincide
-- con SUM(detalle_compra.subtotal) en ninguno de los 4 registros. Además,
-- `detalle_compra` salta de id 5 a id 56 (huecos 6-55), lo que sugiere que
-- existían más partidas de compra que ya no están en el dump. Aquí se
-- conserva el id_detalle_compra original (para no ocultar el hueco) y se
-- recalcula `compras.total` a partir de las partidas realmente disponibles.
-- El total original de cada compra queda documentado en el comentario.
INSERT INTO compras (id_compra, id_proveedor, id_empleado, fecha_compra, total) VALUES
(1, 1, 8, '2025-05-10 10:00:00', 2760.00), -- original en dump: 3500.00 (no reconciliable con las partidas disponibles)
(2, 2, 8, '2025-05-11 12:00:00',  375.00), -- original en dump: 1800.00 (no reconciliable con las partidas disponibles)
(3, 3, 8, '2025-05-12 09:30:00',  500.00), -- original en dump: 2200.00 (no reconciliable con las partidas disponibles)
(4, 1, 8, '2026-05-23 09:15:00', 2440.00); -- original en dump: 2450.00 (diferencia de 10.00, revisar manualmente)

-- Alitas y refresco se convierten a piezas; el costo unitario se ajusta para
-- conservar el valor histórico de cada partida.
INSERT INTO detalle_compra (id_detalle_compra, id_compra, id_insumo, cantidad, costo_unitario) VALUES
(1, 1, 1, 10.00, 180.00),
(2, 1, 6,  160.00, 6.00),
(3, 2, 4,  5.00,  40.00),
(4, 2, 5,  5.00,  35.00),
(5, 3, 10, 33.333, 15.00),
(56, 4, 1,  8.00, 180.00),
(57, 4, 6,  100.00, 6.00),
(58, 4, 2, 20.00,  8.00),
(59, 4, 7,  2.00, 90.00),
(60, 4, 11, 2.00, 30.00);
-- NOTA: ids 6-55 no existen en el dump original; se conservan tal cual para
-- que el hueco quede visible y sea auditable (ver MIGRACION.md).

-- ---------------------------------------------------------------------
-- B.5 Mesas
-- ---------------------------------------------------------------------
INSERT INTO mesas (id_mesa, numero_mesa, capacidad, estado) VALUES
(1, 1, 4, 'Disponible'),
(2, 2, 4, 'Ocupada'),
(3, 3, 6, 'Disponible'),
(4, 4, 2, 'Disponible'),
(5, 5, 8, 'Disponible');

-- ---------------------------------------------------------------------
-- B.6 Órdenes, detalle y pagos
-- ---------------------------------------------------------------------
-- ADVERTENCIA DE MIGRACIÓN: `ordenes.total` en el dump original no coincide
-- con SUM(detalles_orden.subtotal) en NINGUNA de las 18 órdenes. Se recalcula
-- el total a partir de las partidas (regla auditable y documentada) excepto
-- en la orden 18, que no tiene ninguna partida en `detalles_orden`: ahí se
-- conserva el total original porque no hay forma de recalcularlo sin
-- inventar datos. Ver MIGRACION.md para el detalle fila por fila y para la
-- anomalía detectada en `pagos.id_orden` (los montos coinciden sistemáticamente
-- con el total de la orden SIGUIENTE, lo que sugiere un desfase de captura).
--
-- id_tipo_orden = 1 (Local) para todas: en el dump original toda orden con
-- id_mesa tiene mesa asignada, y no existían pedidos para llevar/plataforma.
-- id_usuario_registro = 1 (admin bootstrap): el dump no tenía concepto de
-- usuario/sesión, solo de mesero (id_empleado_atiende).
INSERT INTO ordenes (id_orden, id_tipo_orden, id_mesa, id_cliente, id_empleado_atiende, id_usuario_registro, fecha_orden, id_estado, total) VALUES
(1,  1, 1, 1, 1, 1, '2025-05-15 14:00:00', 5, 310.00), -- original: 300.00
(2,  1, 2, 2, 2, 1, '2025-05-15 15:30:00', 2, 185.00), -- original: 215.00
(3,  1, 3, 3, 3, 1, '2026-05-23 11:29:00', 4, 310.00), -- original: 545.00
(4,  1, 3, 3, 3, 1, '2026-05-23 11:29:00', 4, 255.00), -- original: 320.00
(5,  1, 1, 2, 1, 1, '2026-05-23 12:10:00', 4, 390.00), -- original: 275.00
(6,  1, 4, 4, 2, 1, '2026-05-23 13:05:00', 4, 180.00), -- original: 410.00
(7,  1, 5, 5, 1, 1, '2026-05-23 14:20:00', 4, 510.00), -- original: 180.00
(8,  1, 2, 1, 2, 1, '2026-05-23 15:40:00', 4, 400.00), -- original: 520.00
(9,  1, 1, 3, 1, 1, '2026-05-24 10:15:00', 4, 205.00), -- original: 250.00
(10, 1, 2, 2, 2, 1, '2026-05-24 11:50:00', 4, 360.00), -- original: 340.00
(11, 1, 3, 5, 1, 1, '2026-05-24 13:30:00', 4, 165.00), -- original: 460.00
(12, 1, 4, 1, 2, 1, '2026-05-24 16:10:00', 4, 570.00), -- original: 210.00
(13, 1, 5, 4, 1, 1, '2026-05-24 18:25:00', 4, 320.00), -- original: 390.00
(14, 1, 2, 5, 2, 1, '2026-05-25 09:45:00', 4, 180.00), -- original: 150.00
(15, 1, 1, 1, 1, 1, '2026-05-25 12:00:00', 4, 470.00), -- original: 620.00
(16, 1, 3, 2, 3, 1, '2026-05-25 13:40:00', 4, 380.00), -- original: 310.00
(17, 1, 5, 4, 2, 1, '2026-05-25 17:15:00', 4, 390.00), -- original: 280.00
(18, 1, 4, 3, 1, 1, '2026-05-25 20:05:00', 4, 490.00); -- original: 490.00 (SIN partidas en detalles_orden: revisar manualmente, ver MIGRACION.md)

-- Consecutivo diario visible: se asigna por fecha de la orden y por el
-- orden de captura (id_orden), reiniciando en 1 para cada día.
UPDATE ordenes o
SET numero_orden_diaria = sub.numero
FROM (
    SELECT id_orden,
           ROW_NUMBER() OVER (PARTITION BY DATE(fecha_orden) ORDER BY id_orden) AS numero
    FROM ordenes
) sub
WHERE o.id_orden = sub.id_orden;

-- La columna queda obligatoria para que toda orden nueva tenga su número
-- visible; la aplicación lo asigna al crear la orden.
ALTER TABLE ordenes ALTER COLUMN numero_orden_diaria SET NOT NULL;

INSERT INTO detalles_orden (id_orden, id_producto, cantidad, precio_unitario, id_area_preparacion, id_estado) VALUES
(1, 1, 2, 120.00, 1, 4), (1, 7, 2, 35.00, 2, 4),
(2, 4, 1, 140.00, 1, 2), (2, 8, 1, 45.00, 2, 2),
(3, 1, 2, 120.00, 1, 4), (3, 7, 2, 35.00, 2, 4),
(4, 4, 1, 140.00, 1, 4), (4, 9, 1, 70.00, 2, 4), (4, 8, 1, 45.00, 2, 4),
(5, 3, 2, 155.00, 1, 4), (5, 10, 2, 40.00, 2, 4),
(6, 5, 1, 145.00, 1, 4), (6, 7, 1, 35.00, 2, 4),
(7, 3, 3, 155.00, 1, 4), (7, 8, 1, 45.00, 2, 4),
(8, 1, 1, 120.00, 1, 4), (8, 4, 2, 140.00, 1, 4),
(9, 2, 1, 135.00, 1, 4), (9, 9, 1, 70.00, 2, 4),
(10, 5, 2, 145.00, 1, 4), (10, 7, 2, 35.00, 2, 4),
(11, 1, 1, 120.00, 1, 4), (11, 8, 1, 45.00, 2, 4),
(12, 3, 3, 155.00, 1, 4), (12, 7, 3, 35.00, 2, 4),
(13, 4, 2, 140.00, 1, 4), (13, 10, 1, 40.00, 2, 4),
(14, 2, 1, 135.00, 1, 4), (14, 8, 1, 45.00, 2, 4),
(15, 5, 3, 145.00, 1, 4), (15, 7, 1, 35.00, 2, 4),
(16, 1, 2, 120.00, 1, 4), (16, 9, 2, 70.00, 2, 4),
(17, 3, 2, 155.00, 1, 4), (17, 10, 2, 40.00, 2, 4);
-- La orden 18 se queda intencionalmente sin partidas (ver advertencia arriba).

-- ADVERTENCIA DE MIGRACIÓN: se detectó que `pagos.id_orden` en el dump
-- original parece estar desfasado en una posición: el monto de cada pago
-- (a partir del segundo) coincide con el total ORIGINAL de la orden
-- siguiente a la referenciada, no con la orden indicada. Se cargan los
-- datos EXACTAMENTE como están en el dump (sin reasignar id_orden) porque
-- corregirlo requiere juicio de negocio (saber qué orden cobró realmente
-- cada pago). Revisar manualmente antes de usar estos registros para
-- conciliación financiera real. Ver MIGRACION.md.
INSERT INTO pagos (id_orden, id_metodo_pago, monto, fecha_pago, referencia_pago, id_usuario_registro) VALUES
(1,  1, 310.00, '2025-05-15 14:45:00', 'PAGO001', 1),
(3,  2, 320.00, '2026-05-23 11:50:00', 'PAGO003', 1),
(4,  1, 275.00, '2026-05-23 12:40:00', 'PAGO004', 1),
(5,  3, 410.00, '2026-05-23 13:30:00', 'PAGO005', 1),
(6,  1, 180.00, '2026-05-23 14:45:00', 'PAGO006', 1),
(7,  2, 520.00, '2026-05-23 16:00:00', 'PAGO007', 1),
(8,  2, 250.00, '2026-05-24 10:40:00', 'PAGO008', 1),
(9,  1, 340.00, '2026-05-24 12:10:00', 'PAGO009', 1),
(10, 3, 460.00, '2026-05-24 14:00:00', 'PAGO010', 1),
(11, 1, 210.00, '2026-05-24 16:30:00', 'PAGO011', 1),
(12, 2, 390.00, '2026-05-24 18:50:00', 'PAGO012', 1),
(13, 1, 150.00, '2026-05-25 10:00:00', 'PAGO013', 1),
(14, 2, 620.00, '2026-05-25 12:30:00', 'PAGO014', 1),
(15, 3, 310.00, '2026-05-25 14:00:00', 'PAGO015', 1),
(16, 1, 280.00, '2026-05-25 17:40:00', 'PAGO016', 1),
(17, 2, 490.00, '2026-05-25 20:30:00', 'PAGO017', 1);
-- Nótese que no existe ningún pago con id_orden = 2 en el dump original.

-- ---------------------------------------------------------------------
-- B.7 Movimientos de inventario
-- ---------------------------------------------------------------------
-- id_compra / id_orden se dejan en NULL para los movimientos históricos:
-- el dump original solo traía un texto libre en `motivo`, sin una relación
-- confiable hacia la compra u orden que originó cada movimiento. Los
-- movimientos nuevos generados por la aplicación sí deberán llenar estas
-- referencias.
INSERT INTO movimientos_inventario (id_insumo, id_tipo_movimiento, cantidad, fecha_movimiento, motivo, id_empleado) VALUES
(1,  1, 10.00, '2025-05-10 10:00:00', 'Compra proveedor', 8),
(6,  1,  160.00, '2025-05-10 10:00:00', 'Compra proveedor', 8),
(1,  2,  0.40, '2025-05-15 14:00:00', 'Venta hamburguesas', 3),
(10, 2,  2.00, '2025-05-15 14:00:00', 'Venta refrescos', 5),
(1,  2,  0.40, '2026-05-23 11:40:00', 'Venta hamburguesas', 3),
(2,  2,  2.00, '2026-05-23 11:40:00', 'Uso de pan hamburguesa', 3),
(3,  2,  0.10, '2026-05-23 11:40:00', 'Uso de queso cheddar', 3),
(6,  2,  20.00, '2026-05-23 12:20:00', 'Venta alitas BBQ', 4),
(7,  2,  0.10, '2026-05-23 12:20:00', 'Uso salsa BBQ', 4),
(6,  2,  30.00, '2026-05-24 14:10:00', 'Venta alitas buffalo', 4),
(8,  2,  0.15, '2026-05-24 14:10:00', 'Uso salsa buffalo', 4),
(10, 2,  4.00, '2026-05-24 18:40:00', 'Venta refrescos', 5),
(11, 2,  0.50, '2026-05-25 10:15:00', 'Preparacion limonada', 5),
(12, 2,  0.20, '2026-05-25 17:50:00', 'Preparacion malteada', 5),
(14, 2,  0.60, '2026-05-25 17:50:00', 'Uso leche malteada', 5),
(1,  1, 15.00, '2026-05-26 09:00:00', 'Compra carne res', 8),
(6,  1, 400.00, '2026-05-26 09:00:00', 'Compra alitas pollo', 8),
(2,  1, 50.00, '2026-05-26 09:00:00', 'Compra pan hamburguesa', 8),
(4,  3,  1.00, '2026-05-26 20:00:00', 'Merma lechuga dañada', 8),
(5,  3,  0.50, '2026-05-26 20:00:00', 'Tomate caducado', 8);

-- ---------------------------------------------------------------------
-- B.8 Mermas (detalle de las salidas por pérdida ya registradas arriba)
-- ---------------------------------------------------------------------
-- Estas dos filas ya existían como mermas en el dump original, bajo el tipo
-- genérico 'Merma/Ajuste'; aquí se les agrega su detalle en la nueva tabla
-- `mermas`. Como a los movimientos anteriores no se les fijó un
-- id_movimiento explícito (se dejó que IDENTITY lo asignara), se
-- localiza el registro correcto por su `motivo`, que es único en este seed.
-- costo_estimado_perdida = cantidad * insumos.costo_unitario_referencia
-- (Lechuga: 1.00 * 40.00 = 40.00; Tomate: 0.50 * 35.00 = 17.50).
INSERT INTO mermas (id_movimiento, id_tipo_merma, id_empleado_responsable, id_usuario_registro, descripcion, costo_estimado_perdida, reportado_autoridad, fecha_deteccion)
SELECT id_movimiento, 2, 8, 1, 'Lechuga dañada detectada durante la preparación en cocina', 40.00, FALSE, TIMESTAMP '2026-05-26 20:00:00'
FROM movimientos_inventario WHERE motivo = 'Merma lechuga dañada'
UNION ALL
SELECT id_movimiento, 1, 8, 1, 'Tomate caducado encontrado en refrigerador', 17.50, FALSE, TIMESTAMP '2026-05-26 20:00:00'
FROM movimientos_inventario WHERE motivo = 'Tomate caducado';

-- No se sembraron `tickets` ni `auditoria`: el dump original no llevaba
-- historial de tickets impresos ni bitácora de auditoría, por lo que no hay
-- nada que migrar; ambas tablas quedan listas para que la aplicación las
-- alimente desde el primer día de operación.

-- ---------------------------------------------------------------------
-- Ajuste de secuencias (PostgreSQL)
-- ---------------------------------------------------------------------
-- Los INSERT anteriores fijan ids explícitos. En PostgreSQL las columnas
-- IDENTITY no se mueven solas al insertar un id explícito, así que se
-- sincroniza cada secuencia con el MAX(id) de su tabla para que los
-- INSERT posteriores no fallen con "duplicate key".
SELECT setval(pg_get_serial_sequence('roles', 'id_rol'), COALESCE(MAX(id_rol), 1), MAX(id_rol) IS NOT NULL) FROM roles;
SELECT setval(pg_get_serial_sequence('permisos', 'id_permiso'), COALESCE(MAX(id_permiso), 1), MAX(id_permiso) IS NOT NULL) FROM permisos;
SELECT setval(pg_get_serial_sequence('puestos', 'id_puesto'), COALESCE(MAX(id_puesto), 1), MAX(id_puesto) IS NOT NULL) FROM puestos;
SELECT setval(pg_get_serial_sequence('unidades_medida', 'id_unidad'), COALESCE(MAX(id_unidad), 1), MAX(id_unidad) IS NOT NULL) FROM unidades_medida;
SELECT setval(pg_get_serial_sequence('categorias_insumos', 'id_categoria'), COALESCE(MAX(id_categoria), 1), MAX(id_categoria) IS NOT NULL) FROM categorias_insumos;
SELECT setval(pg_get_serial_sequence('categorias_productos', 'id_categoria'), COALESCE(MAX(id_categoria), 1), MAX(id_categoria) IS NOT NULL) FROM categorias_productos;
SELECT setval(pg_get_serial_sequence('areas_preparacion', 'id_area'), COALESCE(MAX(id_area), 1), MAX(id_area) IS NOT NULL) FROM areas_preparacion;
SELECT setval(pg_get_serial_sequence('metodos_pago', 'id_metodo_pago'), COALESCE(MAX(id_metodo_pago), 1), MAX(id_metodo_pago) IS NOT NULL) FROM metodos_pago;
SELECT setval(pg_get_serial_sequence('estados_orden', 'id_estado'), COALESCE(MAX(id_estado), 1), MAX(id_estado) IS NOT NULL) FROM estados_orden;
SELECT setval(pg_get_serial_sequence('estados_detalle_orden', 'id_estado'), COALESCE(MAX(id_estado), 1), MAX(id_estado) IS NOT NULL) FROM estados_detalle_orden;
SELECT setval(pg_get_serial_sequence('tipos_orden', 'id_tipo_orden'), COALESCE(MAX(id_tipo_orden), 1), MAX(id_tipo_orden) IS NOT NULL) FROM tipos_orden;
SELECT setval(pg_get_serial_sequence('plataformas_externas', 'id_plataforma'), COALESCE(MAX(id_plataforma), 1), MAX(id_plataforma) IS NOT NULL) FROM plataformas_externas;
SELECT setval(pg_get_serial_sequence('tipos_movimiento_inventario', 'id_tipo_movimiento'), COALESCE(MAX(id_tipo_movimiento), 1), MAX(id_tipo_movimiento) IS NOT NULL) FROM tipos_movimiento_inventario;
SELECT setval(pg_get_serial_sequence('tipos_merma', 'id_tipo_merma'), COALESCE(MAX(id_tipo_merma), 1), MAX(id_tipo_merma) IS NOT NULL) FROM tipos_merma;
SELECT setval(pg_get_serial_sequence('empleados', 'id_empleado'), COALESCE(MAX(id_empleado), 1), MAX(id_empleado) IS NOT NULL) FROM empleados;
SELECT setval(pg_get_serial_sequence('usuarios', 'id_usuario'), COALESCE(MAX(id_usuario), 1), MAX(id_usuario) IS NOT NULL) FROM usuarios;
SELECT setval(pg_get_serial_sequence('clientes', 'id_cliente'), COALESCE(MAX(id_cliente), 1), MAX(id_cliente) IS NOT NULL) FROM clientes;
SELECT setval(pg_get_serial_sequence('proveedores', 'id_proveedor'), COALESCE(MAX(id_proveedor), 1), MAX(id_proveedor) IS NOT NULL) FROM proveedores;
SELECT setval(pg_get_serial_sequence('insumos', 'id_insumo'), COALESCE(MAX(id_insumo), 1), MAX(id_insumo) IS NOT NULL) FROM insumos;
SELECT setval(pg_get_serial_sequence('productos', 'id_producto'), COALESCE(MAX(id_producto), 1), MAX(id_producto) IS NOT NULL) FROM productos;
SELECT setval(pg_get_serial_sequence('recetas', 'id_receta'), COALESCE(MAX(id_receta), 1), MAX(id_receta) IS NOT NULL) FROM recetas;
SELECT setval(pg_get_serial_sequence('compras', 'id_compra'), COALESCE(MAX(id_compra), 1), MAX(id_compra) IS NOT NULL) FROM compras;
SELECT setval(pg_get_serial_sequence('detalle_compra', 'id_detalle_compra'), COALESCE(MAX(id_detalle_compra), 1), MAX(id_detalle_compra) IS NOT NULL) FROM detalle_compra;
SELECT setval(pg_get_serial_sequence('mesas', 'id_mesa'), COALESCE(MAX(id_mesa), 1), MAX(id_mesa) IS NOT NULL) FROM mesas;
SELECT setval(pg_get_serial_sequence('ordenes', 'id_orden'), COALESCE(MAX(id_orden), 1), MAX(id_orden) IS NOT NULL) FROM ordenes;
SELECT setval(pg_get_serial_sequence('detalles_orden', 'id_detalle'), COALESCE(MAX(id_detalle), 1), MAX(id_detalle) IS NOT NULL) FROM detalles_orden;
SELECT setval(pg_get_serial_sequence('pagos', 'id_pago'), COALESCE(MAX(id_pago), 1), MAX(id_pago) IS NOT NULL) FROM pagos;
SELECT setval(pg_get_serial_sequence('tickets', 'id_ticket'), COALESCE(MAX(id_ticket), 1), MAX(id_ticket) IS NOT NULL) FROM tickets;
SELECT setval(pg_get_serial_sequence('movimientos_inventario', 'id_movimiento'), COALESCE(MAX(id_movimiento), 1), MAX(id_movimiento) IS NOT NULL) FROM movimientos_inventario;
SELECT setval(pg_get_serial_sequence('mermas', 'id_merma'), COALESCE(MAX(id_merma), 1), MAX(id_merma) IS NOT NULL) FROM mermas;
SELECT setval(pg_get_serial_sequence('auditoria', 'id_auditoria'), COALESCE(MAX(id_auditoria), 1), MAX(id_auditoria) IS NOT NULL) FROM auditoria;
