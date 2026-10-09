"""Taipei All Buses — Streamlit app using TDX stop sequences."""
import math
import os
from collections import defaultdict

import folium
import pandas as pd
import requests
import streamlit as st
from geopy.geocoders import Nominatim
from streamlit_folium import st_folium

st.set_page_config(page_title="AllWays · Taipei buses", page_icon="🚌", layout="wide", initial_sidebar_state="collapsed")

st.markdown("""<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&family=Outfit:wght@400;500;600;700;800&display=swap');
html,body,[class*="css"],.stApp{font-family:'DM Sans',sans-serif;color:#24352f}
.stApp{background:linear-gradient(155deg,#f3f8f4 0%,#fcfdfb 43%,#edf5f2 100%)}
.block-container{max-width:1160px;padding-top:2rem;padding-bottom:3rem}
h1,h2,h3{font-family:'Outfit',sans-serif!important;letter-spacing:-.035em}
#MainMenu,footer{visibility:hidden}
.hero{background:#163e32;border-radius:28px;padding:32px 36px;position:relative;overflow:hidden;color:#fff;margin-bottom:22px}
.hero:after{content:'◉';position:absolute;right:15px;top:-95px;font-size:310px;line-height:1;color:#ffffff0d;pointer-events:none}
.hero .eyebrow{font-weight:800;letter-spacing:.19em;text-transform:uppercase;font-size:11px;color:#b1e0cc}
.hero h1{color:white!important;font-size:clamp(2.4rem,5vw,4rem);line-height:1;margin:14px 0 8px}
.hero p{color:#d5e9dd;font-size:1.04rem;max-width:680px;margin:0}
.hero .chip{display:inline-block;background:#ffffff1c;border:1px solid #ffffff30;border-radius:50px;padding:7px 12px;font-size:12px;margin-top:20px;color:#e4f6ec}
[data-testid="stVerticalBlockBorderWrapper"]{border-radius:20px!important}
[data-testid="stTextInput"] input{background:#fff;border:1px solid #d9e5dd;border-radius:13px;min-height:47px}
.stButton>button[kind="primary"]{background:#d9f67c;color:#163e32;border:0;border-radius:14px;font-weight:800;min-height:51px}
.stButton>button[kind="primary"]:hover{background:#c7eb68;color:#163e32}
[data-testid="stMetric"]{background:white;border:1px solid #dde9df;border-radius:17px;padding:12px 18px}
[data-testid="stMetricValue"]{font-family:'Outfit',sans-serif;font-weight:800;color:#1b543e}
[data-testid="stTabs"] button{font-weight:800}
.note{font-size:13px;color:#52675d;margin:10px 0 18px}
.pill{display:inline-block;background:#e4efe6;border-radius:100px;font-size:12px;font-weight:800;padding:7px 12px;color:#225b41;margin:4px 5px 9px 0}
.route-row{padding:15px 18px;background:white;border:1px solid #dce8df;border-radius:17px;margin:9px 0}
.route-title{color:#193d31;font-size:21px;font-weight:800}
.route-detail{font-size:13px;color:#60766a;line-height:1.65}
.route-badge{font-size:12px;background:#ebf4e8;color:#2b6247;border-radius:8px;padding:4px 8px;vertical-align:middle;margin-left:8px}
.sidebar-hint{background:#edf5ef;padding:12px 15px;border-radius:12px;color:#486355;font-size:13px}
@media(max-width:650px){.block-container{padding:1rem}.hero{padding:25px 22px;border-radius:20px}.hero h1{font-size:2.6rem}}
</style>""", unsafe_allow_html=True)

TDX_ROOT = "https://tdx.transportdata.tw"
UA = "AllWaysTaipei/0.2 (transit research prototype)"


def km_between(a, b):
    lat1, lon1 = a
    lat2, lon2 = b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2-p1, math.radians(lon2-lon1)
    v = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 12742.0176 * math.atan2(math.sqrt(v), math.sqrt(max(0, 1-v)))


def stop_data(s):
    p = s.get("StopPosition") or {}
    try:
        return (float(p["PositionLat"]), float(p["PositionLon"]))
    except (ValueError, TypeError, KeyError):
        return None


def stop_name(s):
    n = s.get("StopName") or {}
    return n.get("Zh_tw") or n.get("En") or "Unknown stop"


@st.cache_data(ttl=3200, show_spinner=False)
def tdx_token(client_id, client_secret):
    r = requests.post(TDX_ROOT + "/auth/realms/TDXConnect/protocol/openid-connect/token",
                      data={"grant_type":"client_credentials", "client_id":client_id, "client_secret":client_secret}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


@st.cache_data(ttl=86400, show_spinner=False)
def get_routes(client_id, client_secret, city):
    token = tdx_token(client_id, client_secret)
    records, skip, page_size = [], 0, 1000
    while True:
        r = requests.get(f"{TDX_ROOT}/api/basic/v2/Bus/StopOfRoute/City/{city}",
                         params={"$top":page_size,"$skip":skip,"$format":"JSON"},
                         headers={"Authorization":f"Bearer {token}","Accept":"application/json"}, timeout=90)
        if r.status_code == 401:
            tdx_token.clear()
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list):
            raise ValueError("Unexpected TDX data format")
        for item in batch:
            item["_city"] = city
        records.extend(batch)
        if len(batch)<page_size:
            break
        skip += page_size
        if skip > 50000:
            raise ValueError("TDX returned unexpectedly many results")
    return records


@st.cache_data(ttl=604800, show_spinner=False)
def locate(place):
    geo = Nominatim(user_agent=UA, timeout=15)
    q = place.strip()
    result = geo.geocode(q + ", Taiwan", country_codes="tw", exactly_one=True)
    return (result.latitude, result.longitude, result.address) if result else None


def prepared(routes, origin, destination, radius):
    """Create direction-specific run records with nearby endpoint indices."""
    out = []
    for record in routes:
        raw = record.get("Stops") or []
        if len(raw)<2:
            continue
        stops = []
        start, end = [], []
        for i, s in enumerate(raw):
            pos = stop_data(s)
            if pos is None:
                stops.append(None)
                continue
            row = {"index":i,"pos":pos,"name":stop_name(s)}
            stops.append(row)
            d0, d1 = km_between(origin, pos), km_between(destination, pos)
            if d0 <= radius:
                start.append((i,d0))
            if d1 <= radius:
                end.append((i,d1))
        if not start and not end:
            continue
        out.append({"record":record,"stops":stops,"start":start,"end":end,
                    "route":(record.get("RouteName") or {}).get("Zh_tw") or (record.get("RouteName") or {}).get("En") or record.get("RouteUID","Unknown"),
                    "direction":record.get("Direction",0)})
    return out


def estimate_min(trip_km, stop_count, walking_m, transfer=False):
    # Heuristic only: 18 km/h, 20s/stop, 4.5 km/h walking, 8min transfer penalty.
    return round(trip_km / 18 * 60 + stop_count / 3 + walking_m / 75 + (8 if transfer else 0))


def direct_routes(runs):
    best = {}
    for run in runs:
        if not run["start"] or not run["end"]:
            continue
        pairs = [(i,j,di,dj) for i,di in run["start"] for j,dj in run["end"] if i < j]
        if not pairs:
            continue
        i,j,di,dj = min(pairs,key=lambda x:x[2]+x[3])
        a,b = run["stops"][i],run["stops"][j]
        if not a or not b:
            continue
        walking = round((di+dj)*1000)
        row = {"routes":[run["route"]],"board":a,"alight":b,"walk_m":walking,"ride_stops":j-i,
               "duration":estimate_min(km_between(a["pos"],b["pos"])*1.3,j-i,walking),"direction":run["direction"],"city":run["record"].get("_city", "")}
        key = (run["route"],run["direction"],a["name"],b["name"])
        if key not in best or walking < best[key]["walk_m"]:
            best[key]=row
    return sorted(best.values(),key=lambda x:(x["walk_m"],x["duration"]))


def transfer_routes(runs, transfer_radius_km=0.22, cap=80):
    """One-transfer alternatives: board first run near source, change nearby, alight near destination."""
    origin_runs = [x for x in runs if x["start"]]
    end_runs = [x for x in runs if x["end"]]
    if not origin_runs or not end_runs:
        return []
    # Spatial grid of possible transfer stops on second runs, excluding stops after final destination.
    grid = defaultdict(list)
    step = 0.0022
    for r_id, run in enumerate(end_runs):
        last = max(i for i,_ in run["end"])
        for idx, stop in enumerate(run["stops"][:last]):
            if stop is None:
                continue
            lat, lon = stop["pos"]
            grid[(math.floor(lat/step),math.floor(lon/step))].append((r_id,idx))
    best = {}
    for first in origin_runs:
        initial = min(i for i,_ in first["start"])
        valid_start = [v for v in first["start"] if v[0] <= initial+8]
        for j in range(initial+1,len(first["stops"])):
            xfer_a = first["stops"][j]
            if xfer_a is None:
                continue
            lat,lon = xfer_a["pos"]
            cell = (math.floor(lat/step), math.floor(lon/step))
            for xx in range(cell[0]-1,cell[0]+2):
                for yy in range(cell[1]-1,cell[1]+2):
                    for r_id,k in grid.get((xx,yy),[]):
                        second = end_runs[r_id]
                        if second["route"] == first["route"]:
                            continue
                        xfer_b = second["stops"][k]
                        gap = km_between(xfer_a["pos"],xfer_b["pos"])
                        if gap > transfer_radius_km:
                            continue
                        ends = [(i,d) for i,d in second["end"] if i>k]
                        starts = [(i,d) for i,d in valid_start if i<j]
                        if not ends or not starts:
                            continue
                        si, sd = min(starts,key=lambda x:x[1])
                        ei, ed = min(ends,key=lambda x:x[1])
                        boarding, alighting = first["stops"][si],second["stops"][ei]
                        if boarding is None or alighting is None:
                            continue
                        walking = round((sd+ed+gap)*1000)
                        ride_km = (km_between(boarding["pos"],xfer_a["pos"]) + km_between(xfer_b["pos"],alighting["pos"])) * 1.3
                        row = {"routes":[first["route"],second["route"]],"board":boarding,"alight":alighting,
                               "transfer_from":xfer_a,"transfer_to":xfer_b,"walk_m":walking,"ride_stops":(j-si)+(ei-k),
                               "duration":estimate_min(ride_km,(j-si)+(ei-k),walking,True),"gap_m":round(gap*1000)}
                        key = (first["route"],second["route"])
                        if key not in best or (row["duration"],row["walk_m"]) < (best[key]["duration"],best[key]["walk_m"]):
                            best[key]=row
    return sorted(best.values(), key=lambda x:(x["duration"],x["walk_m"]))[:cap]


def mini_card(row, index, kind):
    title = " → ".join(row["routes"])
    detail = f"{row['board']['name']} → {row['alight']['name']} · Walk ~{row['walk_m']} m · {row['ride_stops']} stops"
    if kind == "transfer":
        detail += f" · Change at {row['transfer_from']['name']} → {row['transfer_to']['name']} (~{row['gap_m']} m)"
    st.markdown(f'<div class="route-row"><span class="route-title">{title}</span><span class="route-badge">~{row["duration"]} min*</span><div class="route-detail">{detail}</div></div>',unsafe_allow_html=True)


def route_map(origin, destination, chosen):
    center = [(origin[0]+destination[0])/2,(origin[1]+destination[1])/2]
    m = folium.Map(location=center, zoom_start=13,tiles="CartoDB positron",control_scale=True)
    folium.CircleMarker(origin[:2],radius=9,color="#205b43",fill=True,fill_opacity=1,tooltip="Your starting point").add_to(m)
    folium.CircleMarker(destination[:2],radius=9,color="#cd723e",fill=True,fill_opacity=1,tooltip="Your destination").add_to(m)
    bounds=[origin[:2],destination[:2]]
    if chosen:
        points=[origin[:2],chosen["board"]["pos"]]
        folium.PolyLine(points,color="#899e92",weight=3,dash_array="4,7",tooltip="Walking (straight-line schematic)").add_to(m)
        if "transfer_from" in chosen:
            chain=[chosen["board"]["pos"],chosen["transfer_from"]["pos"]]
            folium.PolyLine(chain,color="#20694c",weight=6,tooltip="First bus (schematic)").add_to(m)
            folium.PolyLine([chosen["transfer_from"]["pos"],chosen["transfer_to"]["pos"]],color="#899e92",weight=3,dash_array="4,7",tooltip="Transfer walk").add_to(m)
            folium.PolyLine([chosen["transfer_to"]["pos"],chosen["alight"]["pos"]],color="#cb8549",weight=6,tooltip="Second bus (schematic)").add_to(m)
            for key in ("transfer_from","transfer_to"):
                folium.Marker(chosen[key]["pos"],tooltip=chosen[key]["name"],icon=folium.Icon(color="orange")).add_to(m)
            bounds.extend([chosen["transfer_from"]["pos"],chosen["transfer_to"]["pos"]])
        else:
            folium.PolyLine([chosen["board"]["pos"],chosen["alight"]["pos"]],color="#20694c",weight=6,tooltip="Bus (schematic)").add_to(m)
        folium.PolyLine([chosen["alight"]["pos"],destination[:2]],color="#899e92",weight=3,dash_array="4,7").add_to(m)
        for key,color in (("board","green"),("alight","red")):
            folium.Marker(chosen[key]["pos"],tooltip=chosen[key]["name"],icon=folium.Icon(color=color)).add_to(m)
        bounds.extend([chosen["board"]["pos"],chosen["alight"]["pos"]])
    m.fit_bounds(bounds,padding=[34,34])
    st_folium(m, height=430, use_container_width=True, returned_objects=[])
    st.caption("Map connections are straight-line schematics, NOT bus street paths or turn-by-turn walking directions.")


st.markdown('''<section class="hero"><div class="eyebrow">Taipei + New Taipei · 公車探索</div><h1>Every route.<br>One beautiful map.</h1><p>Find the buses Google Maps doesn't show you. Explore every direct connection, discover one-transfer alternatives, and compare estimated journeys.</p><span class="chip">● Built on official Taiwan TDX bus-stop data</span></section>''',unsafe_allow_html=True)

with st.sidebar:
    st.header("⚙️ Connect to TDX")
    st.markdown('<div class="sidebar-hint">You need a TDX developer account to load real route data. The app does not expose secrets in a public page when deployed securely.</div>',unsafe_allow_html=True)
    client_id = st.text_input("Client ID",value=st.secrets.get("TDX_CLIENT_ID",os.getenv("TDX_CLIENT_ID","")))
    client_secret = st.text_input("Client Secret",value=st.secrets.get("TDX_CLIENT_SECRET",os.getenv("TDX_CLIENT_SECRET","")),type="password")
    include_new_taipei = st.checkbox("Include New Taipei buses",value=True)
    st.markdown("[Get TDX credentials ↗](https://tdx.transportdata.tw/)")

with st.container(border=True):
    col_a,col_b=st.columns(2)
    with col_a:
        origin_query=st.text_input("📍 From",value="Gongguan Station, Taipei",placeholder="MRT, landmark or address")
    with col_b:
        destination_query=st.text_input("🏁 To",value="Wanlong Station, Taipei",placeholder="MRT, landmark or address")
    walk_radius=st.slider("Maximum walk to/from a stop",150,1200,500,50,format="%d m")
    col_btn,col_caption=st.columns([1,1.2],vertical_alignment="center")
    with col_btn:
        go=st.button("Find all possible buses →",type="primary",use_container_width=True)
    with col_caption:
        st.caption("Walking radius uses straight-line distance. A transfer can add up to 220 m of walking.")

if "search_result" not in st.session_state:
    st.session_state.search_result=None

if go:
    if not origin_query.strip() or not destination_query.strip():
        st.error("Enter both your starting point and destination.")
    elif not client_id or not client_secret:
        st.error("Open ⚙️ Connect to TDX in the sidebar and enter your Client ID and Client Secret.")
    else:
        try:
            with st.spinner("Locating your places…"):
                origin,destination=locate(origin_query),locate(destination_query)
            if origin is None or destination is None:
                st.error("Couldn't find one of your places. Try a precise Taipei address or a well-known MRT station.")
            else:
                with st.spinner("Finding all eligible Taipei bus runs…"):
                    raw=get_routes(client_id,client_secret,"Taipei")
                    if include_new_taipei:
                        raw += get_routes(client_id,client_secret,"NewTaipei")
                    runs=prepared(raw,origin[:2],destination[:2],walk_radius/1000)
                    direct=direct_routes(runs)
                with st.spinner("Looking for possible single transfers…"):
                    transfers=transfer_routes(runs)
                st.session_state.search_result={"origin":origin,"destination":destination,"direct":direct,"transfers":transfers,"walk":walk_radius}
        except requests.HTTPError as ex:
            st.error(f"TDX request failed: {ex}. Check your credentials, access permissions and API limits.")
        except Exception as ex:
            st.error(f"Search failed: {ex}")

r=st.session_state.search_result
if r:
    direct,transfers=r["direct"],r["transfers"]
    fastest=sorted(direct+transfers,key=lambda x:(x["duration"],x["walk_m"]))
    st.markdown("### Your connections")
    a,b,c=st.columns(3)
    a.metric("🟢 Direct options",len(direct))
    b.metric("🟡 One-transfer pairs",len(transfers))
    c.metric("🔵 Fastest estimate",f"{fastest[0]['duration']} min*" if fastest else "—")
    st.markdown('<div class="note">*Times are rough model estimates, <b>not</b> live TDX arrival data or a reliable journey planner. Routes may not be operating now. The transfer search returns up to 80 best bus-number pairs, and may omit some possibilities.</div>',unsafe_allow_html=True)
    tabs=st.tabs([f"🟢 Direct · {len(direct)}",f"🟡 One transfer · {len(transfers)}","🔵 Fastest estimates"])
    for tab,items,kind in zip(tabs,[direct,transfers,fastest],["direct","transfer","mixed"]):
        with tab:
            if not items:
                st.info("No matches found with these settings. Try a larger walking radius.")
                continue
            if kind=="direct":
                sort=st.radio("Sort direct connections",["Least walking","Estimated time"],horizontal=True,key="direct_sort")
                shown=sorted(items,key=lambda x:(x["duration"],x["walk_m"])) if sort=="Estimated time" else items
            else:
                shown=items
            choice=st.selectbox("Highlight a journey on the map",range(len(shown)),format_func=lambda i: f"{' → '.join(shown[i]['routes'])} · ~{shown[i]['duration']} min · {shown[i]['walk_m']} m walk",key="select_"+kind)
            route_map(r["origin"],r["destination"],shown[choice])
            st.markdown(f"#### {len(shown)} possible {'bus connections' if kind=='direct' else 'journeys'}")
            for i,row in enumerate(shown[:60]):
                mini_card(row,i,"transfer" if len(row["routes"])==2 else "direct")
            if len(shown)>60:
                st.caption(f"Showing the first 60 of {len(shown)} results.")
else:
    st.markdown("### Discover more ways to get there")
    st.markdown('<span class="pill">01 · Direct routes</span><span class="pill">02 · One-transfer pairs</span><span class="pill">03 · Fastest estimates</span>',unsafe_allow_html=True)
    st.info("To search live published routes: open the sidebar, enter your TDX API credentials, choose two places and press **Find all possible buses**.")
    st.caption("Prototype limitations: no current bus ETAs, service calendar checks, actual bus path polylines, street-network walking routes or reliable departure-based ranking. Public Nominatim geocoding has usage limits.")
