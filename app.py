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

# === XLSX helper (export till Excel) ===
import pandas as pd  # säkerställ att pd finns här

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
        # Fallback om xlsxwriter saknas
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
    st.image("tecomatic.png", use_container_width=True)
    st.header("Så här gör du (MVP)")
    st.markdown(
        """
        1. **Zooma** till dammen.
        2. **Rita dammens yttergräns** med polygon‑verktyget (flerhörning).
        3. **Lägg ut mätpunkter** med marker‑verktyget på de platser där du mätt sedimentdjup.
        4. Under kartan visas en tabell där du fyller i **sedimentdjup (cm)** för varje punkt.
        5. Klicka **Beräkna** för att få area, volym och en enkel rapport.

        **Tips:** Klicka på geolokalisering‑ikonen (mål‑symbolen) uppe till vänster för att visa **var du är** och centrera kartan där.

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
    meas_sigma_cm = st.number_input("Mätosäkerhet per punkt (σ, cm)", 0.0, 50.0, 2.0, 0.5, help="Instrument/avläsning – typiskt 1–5 cm.")
    use_loocv = st.checkbox("Skatta interpolationsfel via LOOCV", value=True, help="Leave-One-Out: för varje punkt uppskattas felet från övriga punkter.")

# --- Karta ---
if "map_center" not in st.session_state:
    # Sverige som start (ungefär mitt)
    st.session_state.map_center = [59.334591, 18.063240]

m = folium.Map(location=st.session_state.map_center, zoom_start=6, control_scale=True)

# Sök på kartan (geokodning via Nominatim) – läggs först så den hamnar högst upp
Geocoder(position='topright', collapsed=True, add_marker=True).add_to(m)

# Visa användarens nuvarande position (webbläsarens geolokalisering)
LocateControl(
    position='topright',
    auto_start=False,
    flyTo=True,
    keepCurrentZoomLevel=True,
    drawCircle=True,
    showPopup=True,
    strings={'title': 'Visa min position', 'popup': 'Du är här (± noggrannhet)'}
).add_to(m)

# Rita-verktyg (polygon + markör)
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
            "shapeOptions": {"color": "#1f77b4"}
        }
    },
    edit_options={"edit": True, "remove": True}
)

draw.add_to(m)

output = st_folium(m, height=600, width=None, returned_objects=["all_drawings", "last_active_drawing"])  # type: ignore(m, height=600, width=None, returned_objects=["all_drawings", "last_active_drawing"])  # type: ignore

# --- Hämta geometrier ---
polygon_geojson = None
points_ll = []  # (lat, lon)

if output:
    drawings = output.get("all_drawings")
    features = []
    if isinstance(drawings, dict) and "features" in drawings:
        features = drawings["features"]
    elif isinstance(drawings, list):
        features = drawings

    # Även fånga senaste aktiva ritningen som enskilt feature
    last_feat = output.get("last_active_drawing")
    if isinstance(last_feat, dict) and last_feat.get("type") == "Feature":
        features.append(last_feat)

    # Gå igenom features och plocka polygoner samt punktmarkörer
    for feat in features:
        geom = feat.get("geometry", {}) if isinstance(feat, dict) else {}
        gtype = geom.get("type")
        if gtype == "Polygon":
            polygon_geojson = feat  # behåll den senast ritade polygonen
        elif gtype == "Point":
            coords = geom.get("coordinates", [])  # GeoJSON: [lon, lat]
            if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                lon, lat = coords[0], coords[1]
                # ibland kommer coords som [lat, lon] – heuristik: lat ∈ [-90,90]
                if abs(lon) <= 90 and abs(lat) <= 180:
                    # troligen [lat, lon], byt plats
                    lat, lon = lon, lat
                points_ll.append((float(lat), float(lon)))

# Visa punktlista och låt användaren ange djup
st.subheader("Mätpunkter")
if len(points_ll) == 0:
    st.info("Lägg ut markörer (mätpunkter) i dammen och ange djup nedan.")

# Deduplicera punkter (kan annars komma dubletter via last_active_drawing/all_drawings)
unique_pts = []
seen = set()
for lat, lon in points_ll:
    key = (round(lat, 6), round(lon, 6))
    if key not in seen:
        seen.add(key)
        unique_pts.append((lat, lon))
points_ll = unique_pts

# Behåll tidigare ifyllda värden per koordinat (stabil default även om index skiftar)
if "depth_by_coord" not in st.session_state:
    st.session_state.depth_by_coord = {}

# Bygg redigerbar tabell för perfekt linjering
if len(points_ll) > 0:
    rows = []
    for lat, lon in points_ll:
        coord_key = f"{lat:.6f}_{lon:.6f}"
        default_val = float(st.session_state.depth_by_coord.get(coord_key, 0.0))
        rows.append({"Latitud": round(lat, 6), "Longitud": round(lon, 6), "Djup (cm)": default_val})
    df = pd.DataFrame(rows, columns=["Latitud", "Longitud", "Djup (cm)"])

    edited = st.data_editor(
        df,
        width='stretch',
        num_rows="fixed",
        hide_index=True,
        column_config={
            "Latitud": st.column_config.NumberColumn(format="%.6f", step=0.000001, disabled=True),
            "Longitud": st.column_config.NumberColumn(format="%.6f", step=0.000001, disabled=True),
            "Djup (cm)": st.column_config.NumberColumn(step=1.0, help="Sedimentdjup i cm för denna punkt"),
        },
        key="points_table",
    )

    # Ladda ner nuvarande mätpunkter som XLSX
    xlsx_bytes_points = build_points_excel(edited)
    st.download_button("Ladda ner mätpunkter (XLSX)", data=xlsx_bytes_points, file_name="matpunkter.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # Läs tillbaka värden + uppdatera session-state
    point_depths_cm = []
    for _, r in edited.iterrows():
        lat = float(r["Latitud"]) ; lon = float(r["Longitud"]) ; dcm = float(r["Djup (cm)"] or 0.0)
        coord_key = f"{lat:.6f}_{lon:.6f}"
        st.session_state.depth_by_coord[coord_key] = dcm
        point_depths_cm.append((lat, lon, dcm))
else:
    point_depths_cm = []

# --- Hjälpfunktioner ---

@st.cache_data(show_spinner=False)
def idw_loocv_rmse_cm(points_ll_depth_cm: list[tuple[float, float, float]], p: float, fwd: Transformer) -> float:
    pts = []
    for lat, lon, d_cm in points_ll_depth_cm:
        x, y = fwd.transform(lon, lat)
        pts.append((x, y, d_cm))
    if len(pts) < 2:
        return 0.0
    xy = np.array([(x, y) for x, y, _ in pts])
    dcm = np.array([d for _, _, d in pts], dtype=float)
    errs = []
    for k in range(len(pts)):
        xk, yk = xy[k]
        others = np.delete(xy, k, axis=0)
        d_others = np.delete(dcm, k, axis=0)
        dx = others[:,0] - xk
        dy = others[:,1] - yk
        dist = np.hypot(dx, dy)
        if np.any(dist == 0):
            pred = float(d_others[dist == 0][0])
        else:
            w = 1.0 / (dist ** p)
            pred = float(np.sum(w * d_others) / np.sum(w))
        errs.append(dcm[k] - pred)
    rmse = float(np.sqrt(np.mean(np.square(errs)))) if errs else 0.0
    return rmse

@st.cache_data(show_spinner=False)
def estimate_neff(points_ll_depth_cm: list[tuple[float, float, float]], fwd: Transformer, res_m: float) -> int:
    """Skatta effektivt antal oberoende punkter med enkel klustring."""
    pts: list[tuple[float, float]] = []
    for lat, lon, _ in points_ll_depth_cm:
        x, y = fwd.transform(lon, lat)
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
