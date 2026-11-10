-- =====================================================================
-- proyectorestaurante — SCHEMA NORMALIZADO (3FN)
-- Motor: InnoDB | Charset: utf8mb4 | Collation: utf8mb4_unicode_ci
-- Compatible con MySQL 8.x y MariaDB 10.4+
-- =====================================================================
-- Convenciones:
--   * Todas las tablas usan InnoDB (transacciones + integridad referencial).
--   * Montos monetarios -> DECIMAL(10,2). Cantidades de insumo -> DECIMAL(12,3)
--     (se admiten fracciones de kg/litro con precisión de gramo/mililitro).
--   * Catálogos controlados en vez de VARCHAR libres, para: roles/permisos,
--     puestos, unidades de medida, métodos de pago, estados de orden/detalle,
--     tipos de orden, plataformas externas, tipos de movimiento de inventario
--     y áreas de preparación. Estados binarios/fijos y poco propensos a
--     crecer (mesas, empleados) se resuelven con ENUM/CHECK para no
--     sobre-normalizar.
--   * La lógica de negocio (descuento de inventario en ventas, cálculo de
--     totales, validaciones cruzadas tipo_orden/mesa, etc.) se implementa en
--     la capa de aplicación (PHP), no con triggers, para mantener el modelo
--     simple, testeable y depurable desde el backend. Ver DISENO.md.
-- =====================================================================

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ---------------------------------------------------------------------
-- 1. CATÁLOGOS DE SEGURIDAD: ROLES Y PERMISOS
-- ---------------------------------------------------------------------

CREATE TABLE roles (
  id_rol        INT UNSIGNED NOT NULL AUTO_INCREMENT,
  nombre        VARCHAR(50)  NOT NULL,
  descripcion   VARCHAR(150) NULL,
  activo        BOOLEAN      NOT NULL DEFAULT TRUE,
  PRIMARY KEY (id_rol),
  UNIQUE KEY uq_roles_nombre (nombre)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE permisos (
  id_permiso    INT UNSIGNED NOT NULL AUTO_INCREMENT,
  clave         VARCHAR(80)  NOT NULL COMMENT 'Identificador único usado por el backend, ej. ordenes.crear',
  descripcion   VARCHAR(150) NULL,
  PRIMARY KEY (id_permiso),
  UNIQUE KEY uq_permisos_clave (clave)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE roles_permisos (
  id_rol      INT UNSIGNED NOT NULL,
  id_permiso  INT UNSIGNED NOT NULL,
  PRIMARY KEY (id_rol, id_permiso),
  CONSTRAINT fk_rp_rol     FOREIGN KEY (id_rol)     REFERENCES roles (id_rol)         ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_rp_permiso FOREIGN KEY (id_permiso) REFERENCES permisos (id_permiso)   ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 2. CATÁLOGOS GENERALES
-- ---------------------------------------------------------------------

CREATE TABLE puestos (
  id_puesto     INT UNSIGNED NOT NULL AUTO_INCREMENT,
  nombre        VARCHAR(50)  NOT NULL,
  descripcion   VARCHAR(150) NULL,
  PRIMARY KEY (id_puesto),
  UNIQUE KEY uq_puestos_nombre (nombre)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE unidades_medida (
  id_unidad     INT UNSIGNED NOT NULL AUTO_INCREMENT,
  nombre        VARCHAR(30)  NOT NULL,
  abreviatura   VARCHAR(10)  NOT NULL,
  tipo_magnitud ENUM('Masa','Volumen','Pieza') NOT NULL COMMENT 'Evita mezclar kg con litros al construir recetas',
  PRIMARY KEY (id_unidad),
  UNIQUE KEY uq_unidad_nombre (nombre),
  UNIQUE KEY uq_unidad_abrev (abreviatura)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE categorias_insumos (
  id_categoria  INT UNSIGNED NOT NULL AUTO_INCREMENT,
  nombre        VARCHAR(50)  NOT NULL,
  descripcion   VARCHAR(150) NULL,
  PRIMARY KEY (id_categoria),
  UNIQUE KEY uq_catinsumo_nombre (nombre)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE categorias_productos (
  id_categoria  INT UNSIGNED NOT NULL AUTO_INCREMENT,
  nombre        VARCHAR(50)  NOT NULL,
  descripcion   VARCHAR(150) NULL,
  PRIMARY KEY (id_categoria),
  UNIQUE KEY uq_catprod_nombre (nombre)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE areas_preparacion (
  id_area   INT UNSIGNED NOT NULL AUTO_INCREMENT,
  nombre    VARCHAR(30)  NOT NULL,
  PRIMARY KEY (id_area),
  UNIQUE KEY uq_area_nombre (nombre)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE metodos_pago (
  id_metodo_pago      INT UNSIGNED NOT NULL AUTO_INCREMENT,
  nombre              VARCHAR(30)  NOT NULL,
  requiere_referencia BOOLEAN      NOT NULL DEFAULT FALSE COMMENT 'Ej. tarjeta/transferencia/plataforma sí, efectivo no',
  activo              BOOLEAN      NOT NULL DEFAULT TRUE,
  PRIMARY KEY (id_metodo_pago),
  UNIQUE KEY uq_metodopago_nombre (nombre)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE estados_orden (
  id_estado    INT UNSIGNED NOT NULL AUTO_INCREMENT,
  nombre       VARCHAR(30)  NOT NULL,
  orden_flujo  TINYINT UNSIGNED NULL COMMENT 'Orden sugerido para mostrar en UI/kanban',
  es_final     BOOLEAN      NOT NULL DEFAULT FALSE COMMENT 'TRUE si ya no debe recibir más cambios (Pagada/Cancelada)',
  PRIMARY KEY (id_estado),
  UNIQUE KEY uq_estadoorden_nombre (nombre)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE estados_detalle_orden (
  id_estado  INT UNSIGNED NOT NULL AUTO_INCREMENT,
  nombre     VARCHAR(30)  NOT NULL,
  es_final   BOOLEAN      NOT NULL DEFAULT FALSE,
  PRIMARY KEY (id_estado),
  UNIQUE KEY uq_estadodet_nombre (nombre)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE tipos_orden (
  id_tipo_orden        INT UNSIGNED NOT NULL AUTO_INCREMENT,
  nombre               VARCHAR(30)  NOT NULL,
  requiere_mesa        BOOLEAN      NOT NULL DEFAULT FALSE,
  requiere_plataforma  BOOLEAN      NOT NULL DEFAULT FALSE,
  PRIMARY KEY (id_tipo_orden),
  UNIQUE KEY uq_tipoorden_nombre (nombre)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE plataformas_externas (
  id_plataforma       INT UNSIGNED NOT NULL AUTO_INCREMENT,
  nombre              VARCHAR(40)  NOT NULL COMMENT 'Rappi, Uber Eats, DiDi Food, etc.',
  comision_porcentaje DECIMAL(5,2) NULL,
  activo              BOOLEAN      NOT NULL DEFAULT TRUE,
  PRIMARY KEY (id_plataforma),
  UNIQUE KEY uq_plataforma_nombre (nombre)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE tipos_movimiento_inventario (
  id_tipo_movimiento INT UNSIGNED NOT NULL AUTO_INCREMENT,
  nombre             VARCHAR(30) NOT NULL COMMENT 'Entrada por compra, Salida por venta, Merma, Ajuste, etc.',
  efecto             ENUM('Entrada','Salida') NOT NULL COMMENT 'Determina si suma o resta al stock_actual',
  PRIMARY KEY (id_tipo_movimiento),
  UNIQUE KEY uq_tipomov_nombre (nombre)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 3. PERSONAS: EMPLEADOS Y USUARIOS (separados a propósito)
-- ---------------------------------------------------------------------

CREATE TABLE empleados (
  id_empleado    INT UNSIGNED NOT NULL AUTO_INCREMENT,
  nombre         VARCHAR(60)  NOT NULL,
  apellido       VARCHAR(60)  NOT NULL,
  telefono       VARCHAR(20)  NULL,
  id_puesto      INT UNSIGNED NOT NULL,
  salario        DECIMAL(10,2) NULL CHECK (salario IS NULL OR salario >= 0),
  fecha_ingreso  DATE NOT NULL,
  fecha_baja     DATE NULL,
  estatus        ENUM('Activo','Inactivo','Baja') NOT NULL DEFAULT 'Activo',
  creado_en      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  actualizado_en TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id_empleado),
  KEY idx_empleado_puesto (id_puesto),
  CONSTRAINT fk_empleado_puesto FOREIGN KEY (id_puesto) REFERENCES puestos (id_puesto) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT chk_empleado_fechas CHECK (fecha_baja IS NULL OR fecha_baja >= fecha_ingreso)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Un usuario PUEDE (no debe) estar ligado a un empleado: permite cuentas de
-- acceso administrativas sin ficha laboral, y empleados sin cuenta.
CREATE TABLE usuarios (
  id_usuario     INT UNSIGNED NOT NULL AUTO_INCREMENT,
  id_empleado    INT UNSIGNED NULL,
  nombre_usuario VARCHAR(50)  NOT NULL,
  correo         VARCHAR(100) NULL,
  password_hash  VARCHAR(255) NOT NULL COMMENT 'Hash bcrypt/argon2 generado en el backend. Nunca texto plano.',
  activo         BOOLEAN      NOT NULL DEFAULT TRUE,
  ultimo_acceso  DATETIME     NULL,
  creado_en      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id_usuario),
  UNIQUE KEY uq_usuario_nombre (nombre_usuario),
  UNIQUE KEY uq_usuario_correo (correo),
  UNIQUE KEY uq_usuario_empleado (id_empleado),
  CONSTRAINT fk_usuario_empleado FOREIGN KEY (id_empleado) REFERENCES empleados (id_empleado) ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE usuarios_roles (
  id_usuario INT UNSIGNED NOT NULL,
  id_rol     INT UNSIGNED NOT NULL,
  PRIMARY KEY (id_usuario, id_rol),
  CONSTRAINT fk_ur_usuario FOREIGN KEY (id_usuario) REFERENCES usuarios (id_usuario) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT fk_ur_rol     FOREIGN KEY (id_rol)     REFERENCES roles (id_rol)       ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 4. CLIENTES Y PROVEEDORES
-- ---------------------------------------------------------------------

CREATE TABLE clientes (
  id_cliente  INT UNSIGNED NOT NULL AUTO_INCREMENT,
  nombre      VARCHAR(100) NOT NULL,
  telefono    VARCHAR(20)  NULL,
  correo      VARCHAR(100) NULL,
  activo      BOOLEAN      NOT NULL DEFAULT TRUE,
  creado_en   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id_cliente)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE proveedores (
  id_proveedor INT UNSIGNED NOT NULL AUTO_INCREMENT,
  nombre       VARCHAR(100) NOT NULL,
  telefono     VARCHAR(20)  NULL,
  direccion    VARCHAR(255) NULL,
  correo       VARCHAR(100) NULL,
  activo       BOOLEAN      NOT NULL DEFAULT TRUE,
  PRIMARY KEY (id_proveedor)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 5. INSUMOS, PRODUCTOS Y RECETAS
-- ---------------------------------------------------------------------

CREATE TABLE insumos (
  id_insumo                INT UNSIGNED NOT NULL AUTO_INCREMENT,
  nombre                   VARCHAR(80)  NOT NULL,
  descripcion              VARCHAR(150) NULL,
  id_categoria             INT UNSIGNED NOT NULL,
  id_unidad_medida         INT UNSIGNED NOT NULL,
  stock_actual             DECIMAL(12,3) NOT NULL DEFAULT 0 CHECK (stock_actual >= 0),
  stock_minimo             DECIMAL(12,3) NOT NULL DEFAULT 0 CHECK (stock_minimo >= 0),
  costo_unitario_referencia DECIMAL(10,2) NULL CHECK (costo_unitario_referencia IS NULL OR costo_unitario_referencia >= 0),
  activo                   BOOLEAN NOT NULL DEFAULT TRUE,
  PRIMARY KEY (id_insumo),
  UNIQUE KEY uq_insumo_nombre (nombre),
  KEY idx_insumo_categoria (id_categoria),
  KEY idx_insumo_unidad (id_unidad_medida),
  CONSTRAINT fk_insumo_categoria FOREIGN KEY (id_categoria)     REFERENCES categorias_insumos (id_categoria) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT fk_insumo_unidad    FOREIGN KEY (id_unidad_medida) REFERENCES unidades_medida (id_unidad)       ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='stock_actual es un valor cacheado; su historial detallado vive en movimientos_inventario, y debe mantenerse sincronizado desde la aplicación.';

CREATE TABLE productos (
  id_producto        INT UNSIGNED NOT NULL AUTO_INCREMENT,
  nombre             VARCHAR(80)  NOT NULL,
  descripcion        VARCHAR(150) NULL,
  id_categoria       INT UNSIGNED NOT NULL,
  id_area_preparacion INT UNSIGNED NOT NULL,
  precio_actual      DECIMAL(10,2) NOT NULL CHECK (precio_actual >= 0),
  disponible         BOOLEAN NOT NULL DEFAULT TRUE COMMENT 'Disponibilidad momentánea (86 / se acabó hoy)',
  activo             BOOLEAN NOT NULL DEFAULT TRUE COMMENT 'Baja lógica permanente del catálogo',
  PRIMARY KEY (id_producto),
  UNIQUE KEY uq_producto_nombre (nombre),
  KEY idx_producto_categoria (id_categoria),
  CONSTRAINT fk_producto_categoria FOREIGN KEY (id_categoria)        REFERENCES categorias_productos (id_categoria) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT fk_producto_area      FOREIGN KEY (id_area_preparacion) REFERENCES areas_preparacion (id_area)         ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE recetas (
  id_receta      INT UNSIGNED NOT NULL AUTO_INCREMENT,
  id_producto    INT UNSIGNED NOT NULL,
  id_insumo      INT UNSIGNED NOT NULL,
  cantidad_usada DECIMAL(10,3) NOT NULL COMMENT 'En la unidad de medida del insumo referenciado' CHECK (cantidad_usada > 0),
  PRIMARY KEY (id_receta),
  UNIQUE KEY uq_receta_producto_insumo (id_producto, id_insumo),
  KEY idx_receta_insumo (id_insumo),
  CONSTRAINT fk_receta_producto FOREIGN KEY (id_producto) REFERENCES productos (id_producto) ON DELETE CASCADE  ON UPDATE CASCADE,
  CONSTRAINT fk_receta_insumo   FOREIGN KEY (id_insumo)   REFERENCES insumos (id_insumo)     ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 6. COMPRAS
-- ---------------------------------------------------------------------

CREATE TABLE compras (
  id_compra    INT UNSIGNED NOT NULL AUTO_INCREMENT,
  id_proveedor INT UNSIGNED NOT NULL,
  id_empleado  INT UNSIGNED NOT NULL COMMENT 'Quién registró la compra',
  fecha_compra DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  total        DECIMAL(10,2) NOT NULL DEFAULT 0 COMMENT 'Debe igualar a SUM(detalle_compra.subtotal); mantenido por la aplicación' CHECK (total >= 0),
  PRIMARY KEY (id_compra),
  KEY idx_compra_proveedor (id_proveedor),
  KEY idx_compra_empleado (id_empleado),
  KEY idx_compra_fecha (fecha_compra),
  CONSTRAINT fk_compra_proveedor FOREIGN KEY (id_proveedor) REFERENCES proveedores (id_proveedor) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT fk_compra_empleado  FOREIGN KEY (id_empleado)  REFERENCES empleados (id_empleado)    ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE detalle_compra (
  id_detalle_compra INT UNSIGNED NOT NULL AUTO_INCREMENT,
  id_compra         INT UNSIGNED NOT NULL,
  id_insumo         INT UNSIGNED NOT NULL,
  cantidad          DECIMAL(12,3) NOT NULL CHECK (cantidad > 0),
  costo_unitario    DECIMAL(10,2) NOT NULL CHECK (costo_unitario >= 0),
  subtotal          DECIMAL(12,2) GENERATED ALWAYS AS (cantidad * costo_unitario) STORED,
  PRIMARY KEY (id_detalle_compra),
  KEY idx_detcompra_compra (id_compra),
  KEY idx_detcompra_insumo (id_insumo),
  CONSTRAINT fk_detcompra_compra FOREIGN KEY (id_compra) REFERENCES compras (id_compra) ON DELETE CASCADE  ON UPDATE CASCADE,
  CONSTRAINT fk_detcompra_insumo FOREIGN KEY (id_insumo) REFERENCES insumos (id_insumo) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='subtotal es columna generada (cantidad*costo_unitario): elimina la redundancia/anomalía de actualización que existía en el dump original.';

-- ---------------------------------------------------------------------
-- 7. MESAS, ÓRDENES, DETALLES Y PAGOS
-- ---------------------------------------------------------------------

CREATE TABLE mesas (
  id_mesa     INT UNSIGNED NOT NULL AUTO_INCREMENT,
  numero_mesa INT UNSIGNED NOT NULL,
  capacidad   INT UNSIGNED NOT NULL CHECK (capacidad > 0),
  estado      ENUM('Disponible','Ocupada','Reservada','Fuera de servicio') NOT NULL DEFAULT 'Disponible',
  PRIMARY KEY (id_mesa),
  UNIQUE KEY uq_mesa_numero (numero_mesa)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE ordenes (
  id_orden            INT UNSIGNED NOT NULL AUTO_INCREMENT,
  id_tipo_orden       INT UNSIGNED NOT NULL,
  id_mesa             INT UNSIGNED NULL COMMENT 'Obligatoria solo cuando tipos_orden.requiere_mesa = TRUE (validado por la app)',
  id_cliente          INT UNSIGNED NULL,
  id_empleado_atiende INT UNSIGNED NULL COMMENT 'Mesero/repartidor asignado',
  id_usuario_registro INT UNSIGNED NOT NULL COMMENT 'Usuario del sistema que capturó la orden (auditoría)',
  id_plataforma_externa INT UNSIGNED NULL COMMENT 'Obligatoria solo cuando tipos_orden.requiere_plataforma = TRUE',
  referencia_externa  VARCHAR(50) NULL COMMENT 'Número de pedido en Rappi/Uber Eats/DiDi Food, si aplica',
  fecha_orden         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  id_estado           INT UNSIGNED NOT NULL,
  total               DECIMAL(10,2) NOT NULL DEFAULT 0 COMMENT 'Debe igualar a SUM(detalles_orden.subtotal); mantenido por la aplicación' CHECK (total >= 0),
  creado_en           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id_orden),
  KEY idx_orden_mesa (id_mesa),
  KEY idx_orden_cliente (id_cliente),
  KEY idx_orden_empleado (id_empleado_atiende),
  KEY idx_orden_estado (id_estado),
  KEY idx_orden_fecha (fecha_orden),
  KEY idx_orden_tipo (id_tipo_orden),
  CONSTRAINT fk_orden_tipo       FOREIGN KEY (id_tipo_orden)         REFERENCES tipos_orden (id_tipo_orden)                 ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT fk_orden_mesa       FOREIGN KEY (id_mesa)               REFERENCES mesas (id_mesa)                             ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT fk_orden_cliente    FOREIGN KEY (id_cliente)            REFERENCES clientes (id_cliente)                       ON DELETE SET NULL  ON UPDATE CASCADE,
  CONSTRAINT fk_orden_empleado   FOREIGN KEY (id_empleado_atiende)   REFERENCES empleados (id_empleado)                     ON DELETE SET NULL  ON UPDATE CASCADE,
  CONSTRAINT fk_orden_usuario    FOREIGN KEY (id_usuario_registro)   REFERENCES usuarios (id_usuario)                       ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT fk_orden_plataforma FOREIGN KEY (id_plataforma_externa) REFERENCES plataformas_externas (id_plataforma)        ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT fk_orden_estado     FOREIGN KEY (id_estado)             REFERENCES estados_orden (id_estado)                   ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE detalles_orden (
  id_detalle          INT UNSIGNED NOT NULL AUTO_INCREMENT,
  id_orden            INT UNSIGNED NOT NULL,
  id_producto         INT UNSIGNED NOT NULL,
  cantidad            INT UNSIGNED NOT NULL CHECK (cantidad > 0),
  precio_unitario     DECIMAL(10,2) NOT NULL COMMENT 'Precio histórico al momento de la venta (no se actualiza si cambia productos.precio_actual)' CHECK (precio_unitario >= 0),
  subtotal            DECIMAL(12,2) GENERATED ALWAYS AS (cantidad * precio_unitario) STORED,
  id_area_preparacion INT UNSIGNED NOT NULL COMMENT 'Copiado de productos.id_area_preparacion al capturar la orden',
  id_estado           INT UNSIGNED NOT NULL,
  PRIMARY KEY (id_detalle),
  KEY idx_detorden_orden (id_orden),
  KEY idx_detorden_producto (id_producto),
  KEY idx_detorden_estado (id_estado),
  CONSTRAINT fk_detorden_orden    FOREIGN KEY (id_orden)            REFERENCES ordenes (id_orden)                     ON DELETE CASCADE  ON UPDATE CASCADE,
  CONSTRAINT fk_detorden_producto FOREIGN KEY (id_producto)         REFERENCES productos (id_producto)                ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT fk_detorden_area     FOREIGN KEY (id_area_preparacion) REFERENCES areas_preparacion (id_area)            ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT fk_detorden_estado   FOREIGN KEY (id_estado)           REFERENCES estados_detalle_orden (id_estado)      ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE pagos (
  id_pago             INT UNSIGNED NOT NULL AUTO_INCREMENT,
  id_orden            INT UNSIGNED NOT NULL,
  id_metodo_pago      INT UNSIGNED NOT NULL,
  monto               DECIMAL(10,2) NOT NULL CHECK (monto > 0),
  fecha_pago          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  referencia_pago     VARCHAR(100) NULL,
  id_usuario_registro INT UNSIGNED NOT NULL,
  PRIMARY KEY (id_pago),
  KEY idx_pago_orden (id_orden),
  KEY idx_pago_metodo (id_metodo_pago),
  CONSTRAINT fk_pago_orden   FOREIGN KEY (id_orden)            REFERENCES ordenes (id_orden)           ON DELETE CASCADE  ON UPDATE CASCADE,
  CONSTRAINT fk_pago_metodo  FOREIGN KEY (id_metodo_pago)      REFERENCES metodos_pago (id_metodo_pago) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT fk_pago_usuario FOREIGN KEY (id_usuario_registro) REFERENCES usuarios (id_usuario)         ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Una orden puede tener varios pagos (cuenta dividida): no existe UNIQUE sobre id_orden.';

CREATE TABLE tickets (
  id_ticket        INT UNSIGNED NOT NULL AUTO_INCREMENT,
  id_orden         INT UNSIGNED NOT NULL,
  folio            VARCHAR(30) NOT NULL,
  tipo             ENUM('Cuenta','Comanda_Cocina','Comanda_Bar','Reimpresion') NOT NULL DEFAULT 'Cuenta',
  id_usuario_emite INT UNSIGNED NOT NULL,
  fecha_emision    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id_ticket),
  UNIQUE KEY uq_ticket_folio (folio),
  KEY idx_ticket_orden (id_orden),
  CONSTRAINT fk_ticket_orden  FOREIGN KEY (id_orden)         REFERENCES ordenes (id_orden)   ON DELETE CASCADE  ON UPDATE CASCADE,
  CONSTRAINT fk_ticket_usuario FOREIGN KEY (id_usuario_emite) REFERENCES usuarios (id_usuario) ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 8. INVENTARIO
-- ---------------------------------------------------------------------

CREATE TABLE movimientos_inventario (
  id_movimiento      INT UNSIGNED NOT NULL AUTO_INCREMENT,
  id_insumo          INT UNSIGNED NOT NULL,
  id_tipo_movimiento INT UNSIGNED NOT NULL,
  cantidad           DECIMAL(12,3) NOT NULL COMMENT 'Siempre positiva; el signo lo da tipos_movimiento_inventario.efecto' CHECK (cantidad > 0),
  fecha_movimiento   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  motivo             VARCHAR(150) NULL,
  id_empleado        INT UNSIGNED NOT NULL COMMENT 'Quién registró el movimiento',
  id_compra          INT UNSIGNED NULL COMMENT 'Referencia opcional si el movimiento proviene de una compra',
  id_orden           INT UNSIGNED NULL COMMENT 'Referencia opcional si el movimiento proviene de una venta',
  PRIMARY KEY (id_movimiento),
  KEY idx_movimiento_insumo (id_insumo),
  KEY idx_movimiento_empleado (id_empleado),
  KEY idx_movimiento_fecha (fecha_movimiento),
  KEY idx_movimiento_compra (id_compra),
  KEY idx_movimiento_orden (id_orden),
  CONSTRAINT fk_movimiento_insumo  FOREIGN KEY (id_insumo)          REFERENCES insumos (id_insumo)                       ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT fk_movimiento_tipo    FOREIGN KEY (id_tipo_movimiento) REFERENCES tipos_movimiento_inventario (id_tipo_movimiento) ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT fk_movimiento_empleado FOREIGN KEY (id_empleado)       REFERENCES empleados (id_empleado)                   ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT fk_movimiento_compra  FOREIGN KEY (id_compra)          REFERENCES compras (id_compra)                       ON DELETE SET NULL ON UPDATE CASCADE,
  CONSTRAINT fk_movimiento_orden   FOREIGN KEY (id_orden)           REFERENCES ordenes (id_orden)                        ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------
-- 8.1 MERMAS Y PÉRDIDAS DE INVENTARIO
-- ---------------------------------------------------------------------
-- Diseño: `mermas` es una tabla SATÉLITE de `movimientos_inventario`, no un
-- registro paralelo. Toda merma debe generar primero su movimiento de
-- salida en `movimientos_inventario` (id_tipo_movimiento -> catálogo
-- 'Merma', efecto Salida) para que el stock se descuente de un único lugar;
-- `mermas` únicamente añade el detalle investigativo/administrativo del
-- incidente (tipo de pérdida, responsable, costo estimado, si se reportó a
-- una autoridad). Ambas filas deben crearse juntas, en la misma transacción,
-- desde el backend (PHP), nunca por separado.

CREATE TABLE tipos_merma (
  id_tipo_merma INT UNSIGNED NOT NULL AUTO_INCREMENT,
  nombre        VARCHAR(40)  NOT NULL COMMENT 'Caducidad, Merma de cocina, Accidente, Robo, Otro',
  requiere_reporte_autoridad BOOLEAN NOT NULL DEFAULT FALSE COMMENT 'Sugerido TRUE para Robo',
  descripcion   VARCHAR(150) NULL,
  PRIMARY KEY (id_tipo_merma),
  UNIQUE KEY uq_tipomerma_nombre (nombre)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE mermas (
  id_merma                INT UNSIGNED NOT NULL AUTO_INCREMENT,
  id_movimiento           INT UNSIGNED NOT NULL COMMENT 'Movimiento de salida en movimientos_inventario que esta merma documenta',
  id_tipo_merma           INT UNSIGNED NOT NULL,
  id_empleado_responsable INT UNSIGNED NULL COMMENT 'Empleado que causó/detectó la pérdida; puede diferir de quien la captura',
  id_usuario_registro     INT UNSIGNED NOT NULL COMMENT 'Usuario del sistema que documentó el reporte',
  descripcion             VARCHAR(255) NOT NULL COMMENT 'Detalle específico del incidente',
  costo_estimado_perdida  DECIMAL(12,2) NULL CHECK (costo_estimado_perdida IS NULL OR costo_estimado_perdida >= 0),
  reportado_autoridad     BOOLEAN NOT NULL DEFAULT FALSE,
  fecha_deteccion         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'Puede diferir de fecha_movimiento si se detecta/reporta después',
  PRIMARY KEY (id_merma),
  UNIQUE KEY uq_merma_movimiento (id_movimiento),
  KEY idx_merma_tipo (id_tipo_merma),
  KEY idx_merma_empleado (id_empleado_responsable),
  KEY idx_merma_usuario (id_usuario_registro),
  CONSTRAINT fk_merma_movimiento FOREIGN KEY (id_movimiento)           REFERENCES movimientos_inventario (id_movimiento) ON DELETE CASCADE  ON UPDATE CASCADE,
  CONSTRAINT fk_merma_tipo       FOREIGN KEY (id_tipo_merma)           REFERENCES tipos_merma (id_tipo_merma)            ON DELETE RESTRICT ON UPDATE CASCADE,
  CONSTRAINT fk_merma_empleado   FOREIGN KEY (id_empleado_responsable) REFERENCES empleados (id_empleado)                ON DELETE SET NULL  ON UPDATE CASCADE,
  CONSTRAINT fk_merma_usuario    FOREIGN KEY (id_usuario_registro)     REFERENCES usuarios (id_usuario)                  ON DELETE RESTRICT ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Detalle de mermas/pérdidas de inventario. id_insumo y cantidad NO se repiten aquí: se obtienen de movimientos_inventario vía id_movimiento.';

-- ---------------------------------------------------------------------
-- 9. AUDITORÍA
-- ---------------------------------------------------------------------

CREATE TABLE auditoria (
  id_auditoria         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  id_usuario           INT UNSIGNED NULL COMMENT 'NULL para acciones automáticas del sistema',
  tabla_afectada       VARCHAR(50) NOT NULL,
  id_registro_afectado INT UNSIGNED NOT NULL,
  accion               ENUM('INSERT','UPDATE','DELETE') NOT NULL,
  datos_anteriores     JSON NULL,
  datos_nuevos         JSON NULL,
  fecha                DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id_auditoria),
  KEY idx_auditoria_usuario (id_usuario),
  KEY idx_auditoria_tabla (tabla_afectada, id_registro_afectado),
  KEY idx_auditoria_fecha (fecha),
  CONSTRAINT fk_auditoria_usuario FOREIGN KEY (id_usuario) REFERENCES usuarios (id_usuario) ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
COMMENT='Bitácora genérica; puede alimentarse desde la aplicación o desde triggers si más adelante se requiere.';

SET FOREIGN_KEY_CHECKS = 1;
