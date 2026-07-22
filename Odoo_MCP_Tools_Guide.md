# Odoo MCP — Guía completa de herramientas y funcionalidad

> Documento de referencia para entender **qué es**, **cómo funciona** y **cómo recrear/enriquecer**
> el servidor MCP de Odoo conectado a esta sesión de Claude.
> Escrito para que cualquier persona (técnica o no) pueda comprenderlo y reconstruirlo.

---

## 1. ¿Qué es este MCP?

**MCP (Model Context Protocol)** es un estándar que permite que un asistente de IA (Claude) hable con
sistemas externos a través de "herramientas" (tools) bien definidas. En lugar de que Claude "adivine",
el servidor MCP expone funciones concretas con parámetros y respuestas estructuradas.

Este servidor en particular es **"Odoo MCP Multi"**, construido por **Nhomar Hernández (Vauxoo**, Odoo
Gold Partner). Su trabajo es traducir peticiones de Claude en llamadas a la **API externa de Odoo** y
devolver los resultados en formatos fáciles de procesar.

Piensa en él como un **puente**:

```
Claude  ──(herramienta MCP)──►  Servidor Odoo MCP  ──(RPC: XML-RPC / JSON-RPC)──►  Odoo (PostgreSQL)
        ◄──(JSON estructurado)──                    ◄──(datos del ORM)──
```

### Características clave
- **Multi-perfil**: puede conectarse a varias instancias de Odoo (producción, QA, dev) por nombre.
- **Acceso al ORM completo**: puede leer, escribir, crear, borrar y ejecutar cualquier método del modelo.
- **Formatos de salida múltiples**: JSON, compacto, tabla Markdown, HTML y CSV.
- **Paginación integrada**: las lecturas grandes devuelven un "sobre" con metadatos de paginación.

---

## 2. Instancia conectada en esta sesión

| Dato | Valor |
|---|---|
| Nombre del perfil | `Interhi` |
| URL | `https://interhi-16-0-qa-34923536.dev.odoo.com` |
| Base de datos | `interhi-16-0-qa-34923536` |
| Versión de Odoo | `16.0+e` (Enterprise) |
| Perfil por defecto | Sí |

> Los perfiles se definen en la **máquina anfitriona** (donde corre el MCP), no dentro de Claude.
> Por eso el primer paso recomendado siempre es `list_available_profiles`.

---

## 3. Modelo mental: cómo funciona Odoo por dentro

Para usar bien estas herramientas conviene entender 4 conceptos de Odoo:

1. **Modelos** = tablas. Ej.: `res.partner` (contactos), `account.move` (facturas/asientos),
   `account.move.line` (líneas), `sale.order` (ventas), `product.product` (productos).
2. **Campos** = columnas. Cada campo tiene un tipo (`char`, `monetary`, `many2one`, `one2many`,
   `selection`, `boolean`…).
3. **Dominios** = filtros de búsqueda, escritos en **notación polaca (prefija)**:
   - Una condición ("leaf") es una lista de 3 elementos: `["campo", "operador", valor]`.
   - Se combinan con operadores lógicos **como prefijo**: `&` (AND), `|` (OR), `!` (NOT).
   - Ejemplos:
     - `[["state", "=", "posted"]]`
     - `["&", ["move_type", "=", "out_invoice"], ["state", "=", "posted"]]`
     - `["|", ["a", "=", 1], ["b", "=", 2]]`
   - ⚠️ **No** se usan las palabras `and`/`or` — dan error `Invalid leaf`.
4. **Métodos** = acciones del modelo. Ej.: `action_post` (validar factura), `button_draft`
   (regresar a borrador), `fields_get` (introspección de campos).

---

## 4. Referencia de las 12 herramientas

Agrupadas por función.

### 🔎 Grupo A — Descubrimiento / introspección

#### 4.1 `list_available_profiles`
Lista las instancias de Odoo configuradas en el host.
- **Parámetros**: ninguno.
- **Devuelve**: arreglo con `name`, `url`, `database`, `is_default`.
- **Cuándo usarlo**: SIEMPRE primero, para saber a qué entorno apuntas.

#### 4.2 `get_version`
Versión del servidor Odoo.
- **Parámetros**: `profile` (opcional).
- **Devuelve**: `server_version` (ej. `16.0+e`), `server_serie`, etc.
- **Útil para**: decidir compatibilidad de métodos/campos según versión.

#### 4.3 `list_models`
Lista los modelos (tablas) disponibles.
- **Parámetros**: `search` (filtro de texto), `format`, `profile`.
- **Devuelve**: `name`, `model`, `info`.
- **Ejemplo**: `search="advance"` para encontrar `l10n_mx_edi.advance`.

#### 4.4 `list_fields`
Lista los campos de un modelo (su "esquema").
- **Parámetros**: `model` (requerido), `attributes` (ej. `"string,type,help,relation,required"`),
  `format`, `profile`.
- **Devuelve**: definición de cada campo.
- **Clave para**: saber qué campos leer/escribir y sus tipos antes de operar.

---

### 📖 Grupo B — Lectura

#### 4.5 `search_read`  ⭐ (el más usado)
Busca y lee registros en un modelo.
- **Parámetros**:
  - `model` (requerido)
  - `domain` — filtro (ver sección 3). Default `[]` (todos).
  - `fields` — lista separada por comas: `"name,amount_total,partner_id"`.
  - `limit` (default 100), `offset` (default 0), `order` (ej. `"id desc"`).
  - `format` — `json` | `compact` | `table` | `html` | `csv`.
  - `profile`.
- **Devuelve**: un **sobre de paginación**:
  ```json
  { "records": [...], "total": 128, "limit": 100, "offset": 0,
    "has_more": true, "next_offset": 100, "format": "json" }
  ```
- **Ejemplo real (esta sesión)** — buscar dos facturas por UUID:
  ```
  model  = account.move
  domain = ["|", ["l10n_mx_edi_cfdi_uuid","=","8BC8..."], ["l10n_mx_edi_cfdi_uuid","=","0994..."]]
  fields = "name,move_type,partner_id,amount_total,state"
  ```

**Guía de `format`:**
| Formato | Úsalo para… |
|---|---|
| `json` | Procesar valores programáticamente (dict por registro). |
| `compact` | Explorar muchos datos (arreglo de arreglos, ~60% más ligero). |
| `table` | Mostrar resúmenes al usuario (Markdown, trunca a 50 chars). |
| `html` | Pegar en el chatter/Knowledge de Odoo (sin truncar). |
| `csv` | Exportar a hoja de cálculo o realimentar a `import_records`. |

#### 4.6 `get_financial_report`
Calcula un reporte financiero nativo (Balance, Estado de Resultados…).
- **Parámetros**: `report_id_or_name` (ID, XML ID o nombre exacto), `date_from`, `date_to`,
  `date_filter` (`today`, `this_month`, `this_year`, `last_month`…), `company_ids`, `format`, `profile`.
- ⚠️ **Compatibilidad**: diseñado para Odoo **17/18/19+**. En Odoo < 17 devuelve error de validación.
  (Esta instancia es 16.0, así que esta herramienta no aplica aquí.)

---

### ✏️ Grupo C — Escritura (mutaciones)

> ⚠️ Estas herramientas **modifican datos reales**. Verifica siempre el entorno (`list_available_profiles`)
> antes de escribir, sobre todo si es producción.

#### 4.7 `create`
Crea un registro.
- **Parámetros**: `model`, `values` (objeto JSON), `profile`.
- **Relacionales** usan "command tuples" de Odoo:
  - one2many / crear línea: `[[0, 0, {"campo": valor}]]`
  - many2many / asignar IDs: `[[6, 0, [id1, id2]]]`
- **Ejemplo real**: crear factura con una línea:
  ```json
  {
    "move_type": "out_invoice", "partner_id": 1126, "journal_id": 1,
    "invoice_line_ids": [[0,0,{"product_id":20579,"quantity":1,"price_unit":1000,
                              "tax_ids":[[6,0,[2]]]}]]
  }
  ```
- **Devuelve**: `{ "success": true, "id": 490749 }`.

#### 4.8 `write`
Actualiza registros existentes.
- **Parámetros**: `model`, `ids` (arreglo o "1,2,3"), `values`, `profile`.
- **Ejemplo real**: poner cuenta de ingreso a un producto:
  ```
  model=product.product  ids=[20579]  values={"property_account_income_id": 32}
  ```

#### 4.9 `unlink`
Borra registros.
- **Parámetros**: `model`, `ids`, `profile`.
- **Devuelve**: `{ "success": true, "deleted_ids": [...] }`.
- ⚠️ Muchos modelos bloquean el borrado (asientos publicados, registros con dependencias).
  Suele requerir primero `button_draft`/`action_cancel` vía `execute_kw`.

#### 4.10 `execute_kw`  ⭐ (la navaja suiza)
Ejecuta **cualquier método** de un modelo. Es la más poderosa y flexible.
- **Parámetros**:
  - `model`, `method`
  - `args` — arreglo posicional (ej. `[[490749]]` = lista de IDs)
  - `kwargs` — objeto (ej. `{"context": {"active_ids": [490752]}}`)
  - `profile`.
- **Ejemplos reales usados en esta sesión**:
  | Objetivo | model | method | args |
  |---|---|---|---|
  | Validar factura | `account.move` | `action_post` | `[[490749]]` |
  | Regresar a borrador | `account.move` | `button_draft` | `[[490754]]` |
  | Opciones de un campo selection | `res.company` | `fields_get` | `[["l10n_mx_edi_advance"]]` |
  | Crear pagos desde wizard | `account.payment.register` | `action_create_payments` | `[[3830]]` |
  | Timbrar CFDI (EDI) | `account.move` | `action_process_edi_web_services` | `[[490749]]` |
- **Por qué importa**: todo lo que un usuario puede hacer con un botón en Odoo, aquí se hace con
  `execute_kw` llamando al método detrás de ese botón.

---

### 📦 Grupo D — Operaciones masivas (ETL)

#### 4.11 `export_records`
Exporta registros con el `export_data` nativo de Odoo (ideal para respaldos/migración).
- **Parámetros**: `model`, `domain`, `fields`, `limit` (default 500), `offset`, `format`, `profile`.
- **Truco**: si pides el campo `id`, Odoo devuelve el **External ID (XML ID)**, estable para reimportar.
  Para relacionales usa la sintaxis de exportación: `"country_id/id"`.

#### 4.12 `import_records`
Importa/actualiza en masa con el `load` nativo de Odoo.
- **Parámetros**: `model`, `fields`, `rows` (arreglo de objetos, mismo formato que `export_records`),
  `profile`.
- **Comportamiento**: si `id` (External ID) existe → **actualiza**; si no → **crea**.
- **Uso típico**: exportar → editar CSV → reimportar (upsert masivo).

---

## 5. Convenciones de respuesta (importantes)

- **Lecturas paginadas** → siempre revisar `has_more`; si es `true`, repetir con `offset = next_offset`.
- **Errores** → el JSON trae `"success": false` y un campo `"error"` con explicación verbosa. Ejemplos
  reales encontrados:
  - `Invalid leaf and` → usaste `"and"` en el dominio; usa `"&"`.
  - `Invalid leaf id` → pasaste `["id","=",x]` en vez de `[["id","=",x]]`.
  - `Expected singleton` → leíste un campo calculado que requiere un solo registro; consúltalo de a uno.
  - `Missing required account on accountable line` → error de negocio de Odoo (no del MCP).
- **Tipos relacionales** en respuestas JSON vienen como `[id, "nombre"]`, ej. `"partner_id": [1126, "PDRICO"]`.

---

## 6. Buenas prácticas y "gotchas" (aprendidas en uso real)

1. **Empieza por descubrir**: `list_available_profiles` → `get_version` → `list_fields`/`list_models`.
2. **Confirma el entorno antes de escribir.** Este servidor apunta a QA; en producción, doble check.
3. **Domains en notación prefija.** Nunca `and`/`or` como palabras.
4. **IDs siempre en lista** para métodos: `args=[[id1, id2]]`.
5. **Contexto vía `kwargs.context`** para wizards (`active_id`, `active_ids`, `active_model`).
6. **Campos calculados**: si dan `Expected singleton`, léelos registro por registro.
7. **Borrar registros contables**: normalmente `button_draft` → limpiar conciliaciones/dependencias → `unlink`.
8. **Usa el `format` correcto**: `table` para enseñar al usuario, `json` para procesar, `csv` para exportar.
9. **Paginación**: no asumas que 100 registros son todos; revisa `total`/`has_more`.

---

## 7. Cómo recrear este servidor (blueprint técnico)

Un servidor MCP de Odoo se construye típicamente así:

1. **Transporte MCP**: usar un framework como **FastMCP (Python)** para exponer tools por `stdio` o HTTP.
2. **Conexión a Odoo**: la **API externa** de Odoo vía `xmlrpc.client` (endpoints
   `/xmlrpc/2/common` para `authenticate`, `/xmlrpc/2/object` para `execute_kw`) o JSON-RPC.
3. **Autenticación**: `common.authenticate(db, user, password/api_key, {})` → devuelve `uid`.
4. **Núcleo**: casi todo se reduce a
   `models.execute_kw(db, uid, key, model, method, args, kwargs)`. Las 12 tools son envolturas
   ergonómicas alrededor de ese método:
   - `search_read` → `execute_kw(model, 'search_read', [domain], {fields, limit, offset, order})`
   - `create` → `execute_kw(model, 'create', [values])`
   - `write` → `execute_kw(model, 'write', [ids, values])`
   - `unlink` → `execute_kw(model, 'unlink', [ids])`
   - `list_fields` → `execute_kw(model, 'fields_get', [], {attributes})`
   - `export_records` → `execute_kw(model, 'export_data', ...)` (o search + read)
   - `import_records` → `execute_kw(model, 'load', [fields, rows])`
5. **Multi-perfil**: un archivo de configuración (`profiles.toml/json`) con credenciales por instancia,
   y un parámetro `profile` en cada tool.
6. **Capa de formato**: función que serializa a `json/compact/table/html/csv`.
7. **Sobre de paginación**: envolver toda lectura con `{records,total,limit,offset,has_more,next_offset}`.
8. **Manejo de errores**: capturar excepciones de Odoo y devolver `{success:false, error:"..."}`.

### Esqueleto mínimo (pseudocódigo Python)
```python
from fastmcp import FastMCP
import xmlrpc.client

mcp = FastMCP("Odoo MCP")

def _client(profile):
    p = PROFILES[profile or DEFAULT]
    common = xmlrpc.client.ServerProxy(f"{p.url}/xmlrpc/2/common")
    uid = common.authenticate(p.db, p.user, p.key, {})
    models = xmlrpc.client.ServerProxy(f"{p.url}/xmlrpc/2/object")
    return models, p.db, uid, p.key

@mcp.tool()
def search_read(model, domain="[]", fields="", limit=100, offset=0, order="", profile=None):
    models, db, uid, key = _client(profile)
    flds = fields.split(",") if fields else []
    rows = models.execute_kw(db, uid, key, model, "search_read",
                             [eval_domain(domain)],
                             {"fields": flds, "limit": limit, "offset": offset, "order": order})
    total = models.execute_kw(db, uid, key, model, "search_count", [eval_domain(domain)])
    return envelope(rows, total, limit, offset)
```

---

## 8. Roadmap de enriquecimiento (tools adicionales sugeridas)

Ideas para hacer el servidor más potente y seguro:

### Lectura / análisis
- **`read_group`** — agregaciones tipo SQL `GROUP BY` (sumas, conteos, promedios) sin traer todo.
  *Falta hoy y es de lo más útil para reportes.*
- **`aggregate` / `pivot`** — tablas dinámicas (medida × dimensiones).
- **`schema_graph`** — mapa de relaciones entre modelos (many2one/one2many) para navegar.
- **`saved_query`** — guardar y reejecutar consultas frecuentes con parámetros.

### Documentos / archivos
- **`get_attachments` / `download_attachment`** — bajar PDFs/XML adjuntos (CFDI, contratos).
- **`render_report`** — generar el PDF de un reporte QWeb (factura, estado de cuenta).
- **`upload_attachment`** — subir archivos al chatter de un registro.

### Flujos de negocio (envolturas seguras sobre `execute_kw`)
- **`post_invoice` / `reset_to_draft` / `register_payment` / `reconcile`** — acciones contables con
  validaciones y mensajes claros.
- **`run_workflow_action`** — ejecutar acciones de servidor/botones por nombre amigable.

### Seguridad y gobernanza
- **`dry_run` / modo simulación** — previsualizar el efecto de un `write`/`create` sin confirmar.
- **`permission_check`** — verificar reglas de acceso (`check_access_rights`) antes de operar.
- **`audit_log`** — registrar quién/qué/cuándo para toda mutación.
- **`readonly_profiles`** — marcar perfiles de producción como solo-lectura salvo confirmación explícita.
- **`transaction` / batch atómico** — agrupar varias operaciones con rollback si algo falla.

### Calidad de vida
- **`explain_error`** — traducir errores crudos de Odoo a explicaciones accionables.
- **`field_picker`** — sugerir campos relevantes de un modelo (con descripciones).
- **`export_to_drive` / `to_xlsx`** — exportar resultados directo a Excel/Drive.

---

## 9. Resumen ejecutivo

| Herramienta | Grupo | Riesgo | Uso principal |
|---|---|---|---|
| `list_available_profiles` | Descubrir | — | Ver entornos |
| `get_version` | Descubrir | — | Versión de Odoo |
| `list_models` | Descubrir | — | Encontrar tablas |
| `list_fields` | Descubrir | — | Ver esquema de una tabla |
| `search_read` | Leer | — | **Consultar datos** |
| `get_financial_report` | Leer | — | Reportes (Odoo 17+) |
| `create` | Escribir | ⚠️ | Crear registros |
| `write` | Escribir | ⚠️ | Actualizar registros |
| `unlink` | Escribir | ⚠️⚠️ | Borrar registros |
| `execute_kw` | Escribir | ⚠️⚠️ | **Ejecutar cualquier método** |
| `export_records` | Masivo | — | Exportar (backup/migración) |
| `import_records` | Masivo | ⚠️ | Importar/actualizar en masa |

**En una frase:** este MCP convierte a Claude en un operador del ORM de Odoo — puede descubrir el
esquema, consultar con filtros y formatos, mutar datos y ejecutar cualquier acción de negocio — todo a
través de 12 herramientas que envuelven la API externa de Odoo; y puede enriquecerse con agregaciones,
manejo de archivos, flujos contables seguros y controles de gobernanza.

---

*Fuente del servidor: Odoo MCP Multi — Nhomar Hernández / Vauxoo · https://vauxoo.com*
*Documento generado a partir de la introspección directa de las 12 herramientas y de su uso real en esta sesión.*
