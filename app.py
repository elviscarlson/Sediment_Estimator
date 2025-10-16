import streamlit as st
from streamlit_folium import st_folium
import folium
from folium.plugins import Draw, LocateControl, Geocoder
from shapely.geometry import shape, Point, Polygon
from shapely.ops import transform as shp_transform
import numpy as np
from pyproj import Transformer, CRS
import math
import json
import pandas as pd
import io
import csv  # Lägg till denna import här i toppen!

# === XLSX helper (export till Excel) ===
def build_points_excel(df_points: pd.DataFrame, report: dict | None = None) -> bytes:
    """Bygger en XLSX som innehåller bladet 'Matpunkter' och valfritt 'Rapport'."""
    buf = io.BytesIO()
    try:
        with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
            df_points.to_excel(writer, index=False, sheet_name="Matpunkter")
            if report is not None:
                rep_df = pd.DataFrame({"Nyckel": list(report.keys()), "Värde": list(report.values())})
                rep_df.to_excel(writer, index=False, sheet_name="Rapport")
    except Exception:
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df_points.to_excel(writer, index=False, sheet_name="Matpunkter")
            if report is not None:
                rep_df = pd.DataFrame({"Nyckel": list(report.keys()), "Värde": list(report.values())})
                rep_df.to_excel(writer, index=False, sheet_name="Rapport")
    buf.seek(0)
    return buf.getvalue()

st.set_page_config(page_title="SedimentEstimator", layout="wide")
st.title("SedimentEstimator – uppskatta sedimentvolym i dammar")

with st.sidebar:
    st.image("tecomatic.png", width=220)
    st.header("Så här gör du (MVP)")
    st.markdown(
        """
        1. **Zooma** till dammen.
        2. **Rita dammens yttergräns** med polygon‑verktyget (flerhörning).
        3. **Lägg ut mätpunkter** med marker‑verktyget på de platser där du mätt sedimentdjup.
        4. Under kartan visas en tabell där du fyller i **sedimentdjup (cm)** för varje punkt.
        5. Klicka **Beräkna** för att få area, volym och en enkel rapport.

        **Tips för mobil:** 
        - För att stänga polygonen: Tryck på första punkten ELLER använd "Finish"-knappen
        - Zooma in för bättre precision vid touchkontroller
        
        **Tips:** Klicka på geolokalisering‑ikonen (mål‑symbolen) för att visa **var du är**.

        **Antaganden:**
        - Volymen beräknas via ett finmaskigt rutnät (*IDW-interpolation*) inom polygonen.
        - Djupet antas vara medeldjup i närområdet kring varje punkt.
        - Koordinater projiceras automatiskt till UTM-zon för korrekta area/volym.

        **Tips:** För bästa noggrannhet, lägg punkter där djupet varierar.
        """
    )
    default_power = st.number_input("IDW‑exponent (p)", 1.0, 4.0, 2.0, 0.5, help="Hur snabbt inflytandet från en punkt avtar med avstånd.")
    target_cells = st.number_input("Riktvärde rutnätsceller", 2000, 50000, 10000, 1000, help="Används för att välja rutnätsupplösning.")
    st.divider()
    st.subheader("Felmarginaler")
    include_uncert = st.checkbox("Beräkna felmarginaler (95% CI)", value=True)
    meas_sigma_cm = st.number_input(
        "Mätosäkerhet per punkt (σ, cm)",
        min_value=0.0, max_value=50.0, value=2.0, step=0.5,
        help="Instrument/avläsning – typiskt 1–5 cm."
    )
    use_loocv = st.checkbox(
        "Skatta interpolationsfel via LOOCV",
        value=True,
        help="Leave-One-Out: varje punkt förutsägs av de övriga punkterna → globalt RMSE i cm."
    )

# --- Karta ---
if "map_center" not in st.session_state:
    st.session_state.map_center = [59.334591, 18.063240]

m = folium.Map(
    location=st.session_state.map_center, 
    zoom_start=6, 
    control_scale=True,
    tiles='OpenStreetMap'
)

# Förbättrad CSS för mobil-support
st.markdown("""
<style>
/* Gör Draw-kontrollerna mer touch-vänliga */
.leaflet-draw-toolbar a {
    width: 40px !important;
    height: 40px !important;
    line-height: 40px !important;
}

/* Större och synligare Finish/Cancel/Delete knappar */
.leaflet-draw-actions {
    z-index: 10000 !important;
}

.leaflet-draw-actions a {
    height: 36px !important;
    line-height: 36px !important;
    padding: 0 12px !important;
    font-size: 14px !important;
    font-weight: 600 !important;
}

/* Gör redigerings-handtag större och lättare att trycka på */
.leaflet-editing-icon {
    width: 20px !important;
    height: 20px !important;
    margin-left: -10px !important;
    margin-top: -10px !important;
    border: 3px solid #fff !important;
    background-color: #b41f1f !important;
}

/* Första punkten extra stor för att lättare stänga polygonen */
.leaflet-marker-icon.leaflet-div-icon.leaflet-editing-icon:first-child {
    width: 24px !important;
    height: 24px !important;
    margin-left: -12px !important;
    margin-top: -12px !important;
    background-color: #4CAF50 !important;
}

/* Justera position på mobil */
@media (max-width: 768px) {
    .leaflet-control-container .leaflet-left { 
        left: 10px !important; 
    }
    .leaflet-control-container .leaflet-top { 
        top: 10px !important; 
    }
    .leaflet-control-container .leaflet-right { 
        right: 10px !important; 
    }
    
    .leaflet-draw-toolbar {
        margin-top: 10px !important;
    }
}

.leaflet-top.leaflet-right .leaflet-control {
    z-index: 500 !important;
    margin-top: 10px !important;
}

.leaflet-top.leaflet-left .leaflet-control {
    z-index: 1000 !important;
}

.leaflet-interactive {
    stroke-width: 3px !important;
}
</style>
""", unsafe_allow_html=True)

Geocoder(
    position='topright', 
    collapsed=True, 
    add_marker=True,
    placeholder='Sök plats...'
).add_to(m)

LocateControl(
    position='topright',
    auto_start=False,
    flyTo=True,
    keepCurrentZoomLevel=False,
    drawCircle=True,
    showPopup=True,
    strings={'title': 'Visa min position', 'popup': 'Du är här (± noggrannhet)'}
).add_to(m)

draw = Draw(
    draw_options={
        "polyline": False,
        "rectangle": False,
        "circle": False,
        "circlemarker": False,
        "marker": True,
        "polygon": {
            "allowIntersection": False,
            "showArea": True,
            "shapeOptions": {
                "color": "#b41f1f",
                "weight": 3,
                "fillOpacity": 0.2
            },
            "drawError": {
                "color": "#e1e100",
                "message": "<strong>Obs!</strong> Polygonen korsar sig själv!"
            },
            "icon": None,
            "touchIcon": None,
            "repeatMode": False
        }
    },
    edit_options={
        "edit": True, 
        "remove": True,
        "poly": {
            "allowIntersection": False
        }
    },
    position='topleft'
)

draw.add_to(m)

output = st_folium(
    m, 
    height=600, 
    width=None, 
    returned_objects=["all_drawings", "last_active_drawing"],
    key="folium_map"
)

# --- Hämta geometrier ---
polygon_geojson = None
points_ll = []

if output:
    drawings = output.get("all_drawings")
    features = []
    if isinstance(drawings, dict) and "features" in drawings:
        features = drawings["features"]
    elif isinstance(drawings, list):
        features = drawings

    last_feat = output.get("last_active_drawing")
    if isinstance(last_feat, dict) and last_feat.get("type") == "Feature":
        features.append(last_feat)

    for feat in features:
        geom = feat.get("geometry", {}) if isinstance(feat, dict) else {}
        gtype = geom.get("type")
        if gtype == "Polygon":
            polygon_geojson = feat
        elif gtype == "Point":
            coords = geom.get("coordinates", [])
            if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                lon, lat = coords[0], coords[1]
                if abs(lon) <= 90 and abs(lat) <= 180:
                    lat, lon = lon, lat
                points_ll.append((float(lat), float(lon)))

st.info("💡 **Mobiltips:** För att stänga polygonen, tryck på den första punkten (grön) eller använd 'Finish'-knappen som dyker upp under verktygsikonerna.")

st.subheader("Mätpunkter")
if len(points_ll) == 0:
    st.info("Lägg ut markörer (mätpunkter) i dammen och ange djup nedan.")

unique_pts = []
seen = set()
for lat, lon in points_ll:
    key = (round(lat, 6), round(lon, 6))
    if key not in seen:
        seen.add(key)
        unique_pts.append((lat, lon))
points_ll = unique_pts

if "depth_by_coord" not in st.session_state:
    st.session_state.depth_by_coord = {}

if len(points_ll) > 0:
    rows = []
    for lat, lon in points_ll:
        coord_key = f"{lat:.6f}_{lon:.6f}"
        default_val = float(st.session_state.depth_by_coord.get(coord_key, 0.0))
        rows.append({"Latitud": round(lat, 6), "Longitud": round(lon, 6), "Djup (cm)": default_val})
    df = pd.DataFrame(rows, columns=["Latitud", "Longitud", "Djup (cm)"])

    edited = st.data_editor(
        df,
        use_container_width=True,
        num_rows="fixed",
        hide_index=True,
        column_config={
            "Latitud": st.column_config.NumberColumn(format="%.6f", step=0.000001, disabled=True),
            "Longitud": st.column_config.NumberColumn(format="%.6f", step=0.000001, disabled=True),
            "Djup (cm)": st.column_config.NumberColumn(step=1.0, help="Sedimentdjup i cm för denna punkt"),
        },
        key="points_table",
    )

    point_depths_cm = []
    for _, r in edited.iterrows():
        lat = float(r["Latitud"])
        lon = float(r["Longitud"])
        dcm = float(r["Djup (cm)"] or 0.0)
        coord_key = f"{lat:.6f}_{lon:.6f}"
        st.session_state.depth_by_coord[coord_key] = dcm
        point_depths_cm.append((lat, lon, dcm))
else:
    point_depths_cm = []

# --- Hjälpfunktioner ---

def pick_utm_epsg(lat: float, lon: float) -> int:
    zone = int((lon + 180) // 6) + 1
    is_north = lat >= 0
    return 32600 + zone if is_north else 32700 + zone

@st.cache_data(show_spinner=False)
def to_utm_transformer(lat: float, lon: float):
    epsg = pick_utm_epsg(lat, lon)
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    inv_transformer = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    return epsg, transformer, inv_transformer

@st.cache_data(show_spinner=False)
def polygon_area_m2(polygon_gj: dict) -> tuple[float, Polygon, Transformer]:
    poly = shape(polygon_gj["geometry"])
    lon, lat = poly.representative_point().x, poly.representative_point().y
    epsg, fwd, _ = to_utm_transformer(lat, lon)
    poly_m = shp_transform(lambda x, y, z=None: fwd.transform(x, y), poly)
    return poly_m.area, poly_m, fwd

@st.cache_data(show_spinner=False)
def idw_loocv_rmse_cm(
    points_ll_depth_cm: list[tuple[float, float, float]],
    p: float,
    _fwd: Transformer,
) -> float:
    if len(points_ll_depth_cm) < 2:
        return 0.0

    xy = []
    dcm = []
    for lat, lon, d_cm in points_ll_depth_cm:
        x, y = _fwd.transform(lon, lat)
        xy.append((x, y))
        dcm.append(float(d_cm))
    xy = np.array(xy, dtype=float)
    dcm = np.array(dcm, dtype=float)

    errs = []
    for k in range(len(dcm)):
        others_xy = np.delete(xy, k, axis=0)
        others_d  = np.delete(dcm, k, axis=0)
        dx = others_xy[:, 0] - xy[k, 0]
        dy = others_xy[:, 1] - xy[k, 1]
        dist = np.hypot(dx, dy)
        if np.any(dist == 0):
            pred = float(others_d[dist == 0][0])
        else:
            w = 1.0 / (dist ** p)
            pred = float(np.sum(w * others_d) / np.sum(w))
        errs.append(dcm[k] - pred)

    return float(np.sqrt(np.mean(np.square(errs)))) if errs else 0.0

@st.cache_data(show_spinner=False)
def estimate_neff(
    points_ll_depth_cm: list[tuple[float, float, float]],
    _fwd: Transformer,
    res_m: float,
) -> int:
    pts = []
    for lat, lon, _ in points_ll_depth_cm:
        x, y = _fwd.transform(lon, lat)
        pts.append((x, y))
    n = len(pts)
    if n == 0:
        return 0

    parent = list(range(n))
    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    thresh = 2.0 * max(res_m, 0.5)
    for i in range(n):
        for j in range(i + 1, n):
            if math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]) <= thresh:
                union(i, j)

    comps = len({find(i) for i in range(n)})
    return max(1, comps)

@st.cache_data(show_spinner=False)
def idw_volume(points_ll_depth_cm: list[tuple[float, float, float]], polygon_gj: dict, p: float, target_cells: int):
    area_m2, poly_m, fwd = polygon_area_m2(polygon_gj)
    if area_m2 <= 0:
        return 0.0, area_m2, 0.0, None

    pts_m = []
    for lat, lon, d_cm in points_ll_depth_cm:
        x, y = fwd.transform(lon, lat)
        pts_m.append((x, y, d_cm / 100.0))

    if len(pts_m) == 0:
        return 0.0, area_m2, 0.0, None

    minx, miny, maxx, maxy = poly_m.bounds
    bbox_area = (maxx - minx) * (maxy - miny)
    cell_area_target = max(bbox_area / target_cells, 0.25)
    res = math.sqrt(cell_area_target)
    res = min(max(res, 0.5), 5.0)

    nx = int(math.ceil((maxx - minx) / res))
    ny = int(math.ceil((maxy - miny) / res))

    xs = np.linspace(minx + res/2, minx + res/2 + (nx-1)*res, nx)
    ys = np.linspace(miny + res/2, miny + res/2 + (ny-1)*res, ny)

    vol = 0.0
    cell_area = res * res
    nearest_idx = np.full((ny, nx), -1, dtype=int)

    pts_xy = np.array([(x, y) for x, y, _ in pts_m])
    pts_d = np.array([d for _, _, d in pts_m])

    for j, y in enumerate(ys):
        for i, x in enumerate(xs):
            if not poly_m.contains(Point(x, y)):
                continue
            dx = pts_xy[:, 0] - x
            dy = pts_xy[:, 1] - y
            dist = np.hypot(dx, dy)

            if np.any(dist == 0):
                d = pts_d[dist == 0][0]
                nearest_idx[j, i] = int(np.where(dist == 0)[0][0])
            else:
                w = 1.0 / (dist ** p)
                d = float(np.sum(w * pts_d) / np.sum(w))
                nearest_idx[j, i] = int(np.argmin(dist))

            vol += d * cell_area

    return vol, area_m2, res, nearest_idx

# --- Beräkning ---
col_a, col_b = st.columns([1, 1])
with col_a:
    calc = st.button("Beräkna volym", type="primary", use_container_width=True)

report = {}
if calc:
    if polygon_geojson is None:
        st.error("Du måste rita dammens yttergräns (polygon) först.")
    elif len(point_depths_cm) < 1:
        st.error("Lägg minst en mätpunkt och ange djup.")
    else:
        with st.spinner("Beräknar…"):
            vol_m3, area_m2, res_m, _ = idw_volume(point_depths_cm, polygon_geojson, default_power, int(target_cells))
        st.success("Klart!")

        mean_depth = vol_m3 / area_m2 if area_m2 > 0 else 0.0
        
        ci_depth = None
        ci_vol = None
        rmse_interp_cm = 0.0
        neff = 0
        
        if include_uncert:
            _, _, fwd = polygon_area_m2(polygon_geojson)
            rmse_interp_cm = idw_loocv_rmse_cm(point_depths_cm, default_power, fwd) if use_loocv else 0.0
            sigma_point_cm = float(np.hypot(meas_sigma_cm, rmse_interp_cm))
            neff = estimate_neff(point_depths_cm, fwd, res_m)
            se_mean_depth_m = (sigma_point_cm / 100.0) / math.sqrt(max(1, neff))
            delta = 1.96 * se_mean_depth_m
            ci_depth = (max(0.0, mean_depth - delta), mean_depth + delta)
            ci_vol = (ci_depth[0] * area_m2, ci_depth[1] * area_m2)

        report = {
            "Dammens area (m²)": float(area_m2),
            "Beräknad volym sediment (m³)": float(vol_m3),
            "Beräknat medeldjup (m)": float(mean_depth),
            "Rutnätsupplösning (m)": float(res_m),
            "Antal mätpunkter": len(point_depths_cm),
        }
        
        if include_uncert:
            report.update({
                "Mätosäkerhet σ (cm)": float(meas_sigma_cm),
                "Interpolations-RMSE (cm)": float(rmse_interp_cm),
                "Effektivt antal punkter (n_eff)": int(neff),
                "95% CI medeldjup (m)": f"{ci_depth[0]:.2f}–{ci_depth[1]:.2f}" if ci_depth else "N/A",
                "95% CI volym (m³)": f"{ci_vol[0]:,.0f}–{ci_vol[1]:,.0f}" if ci_vol else "N/A",
            })

        st.subheader("Resultat")
        col1, col2 = st.columns(2)
        with col1:
            st.metric("Dammens area", f"{area_m2:,.0f} m²")
            st.metric("Medeldjup", f"{mean_depth:.2f} m")
            if include_uncert and ci_depth:
                st.caption(f"95% CI medeldjup: {ci_depth[0]:.2f}–{ci_depth[1]:.2f} m")
        with col2:
            st.metric("Volym sediment", f"{vol_m3:,.0f} m³")
            st.metric("Rutnätsupplösning", f"{res_m:.2f} m")
            if include_uncert and ci_vol:
                st.caption(f"95% CI volym: {ci_vol[0]:,.0f}–{ci_vol[1]:,.0f} m³")

# Ladda ner rapport som JSON
rep_json = json.dumps(report, indent=2, ensure_ascii=False)
st.download_button("Ladda ner rapport (JSON)", data=rep_json, file_name="sediment_rapport.json", mime="application/json")

# Ladda ner mätpunkter som CSV
csv_buf = io.StringIO()
csv_writer = csv.writer(csv_buf)  # Ändrat från writer till csv_writer
csv_writer.writerow(["lat", "lon", "djup_cm"])
for lat, lon, dcm in point_depths_cm:
    csv_writer.writerow([lat, lon, dcm])
st.download_button("Ladda ner mätpunkter (CSV)", data=csv_buf.getvalue(), file_name="matpunkter.csv", mime="text/csv")

# Ladda ner rapport + mätpunkter som XLSX
if len(point_depths_cm) > 0:
    points_df_for_xlsx = pd.DataFrame(point_depths_cm, columns=["Latitud", "Longitud", "Djup (cm)"])
    xlsx_bytes_full = build_points_excel(points_df_for_xlsx, report=report)
    st.download_button("Ladda ner rapport + mätpunkter (XLSX)", data=xlsx_bytes_full, file_name="sediment_berakning.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

st.caption("MVP v0.2 – Leaflet/Folium + IDW. Byggd för fältbruk med Streamlit.")
