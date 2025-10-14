import streamlit as st
from openai import AzureOpenAI
import requests
import re
import os

# Streamlit page config MUST be first
st.set_page_config(page_title="TruthTrace Fact Checker", page_icon="🔍", layout="wide")

# Load credentials from Streamlit secrets or environment variables
def get_secret(key, default=None):
    """Get secret from Streamlit secrets or environment variables"""
    try:
        return st.secrets[key]
    except:
        return os.getenv(key, default)

# Initialize Azure OpenAI client - FIXED VERSION
@st.cache_resource
def get_openai_client():
    """Cache the OpenAI client to avoid recreating it"""
    return AzureOpenAI(
        api_key=get_secret("AZURE_OPENAI_KEY"),
        azure_endpoint=get_secret("AZURE_OPENAI_ENDPOINT"),
        # REMOVED: api_version parameter - it's not needed in newer versions
    )

client = get_openai_client()
DEPLOYMENT_NAME = get_secret("AZURE_OPENAI_DEPLOYMENT", "gpt-4")
SERPER_API_KEY = get_secret("SERPER_API_KEY")
PIXABAY_API_KEY = get_secret("PIXABAY_API_KEY")
YOUTUBE_API_KEY = get_secret("YOUTUBE_API_KEY")

def extract_main_keyword(claim):
    """Extract the main subject from the claim"""
    splitters = [' who ', ' that ', ' which ', ' is ', ' has ',
                 ' was ', ' were ', ' won ', ' scored ', ' can ']
    cl = claim.lower()
    for s in splitters:
        if s in cl:
            return claim[:cl.index(s)].strip()
    return claim.strip()

@st.cache_data(ttl=300)
def serper_search_with_top_sources_and_images(query, top_n=4):
    """Search Google using Serper API for sources and images"""
    url = "https://google.serper.dev/search"
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    sources, images = [], []
    try:
        resp = requests.post(url, headers=headers,
                             json={"q": query, "gl": "us", "hl": "en"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "organic" in data:
            for r in data["organic"][:top_n]:
                sources.append({
                    "title": r.get("title", ""),
                    "link": r.get("link", ""),
                    "snippet": r.get("snippet", ""),
                    "link_lower": r.get("link", "").lower()
                })
        if "images" in data and data["images"]:
            for img in data["images"][:10]:
                img_url = img.get("thumbnail") or img.get("url")
                if img_url:
                    images.append(img_url)
    except Exception as e:
        st.error(f"Failed to fetch search results: {str(e)}")
    return sources, images

def fetch_wikipedia_infobox_image(wikipedia_url):
    """Extract infobox image from Wikipedia article"""
    try:
        title = wikipedia_url.split("/wiki/")[-1].replace("_", " ")
        api_url = "https://en.wikipedia.org/w/api.php"
        params = {
            "action": "query", "titles": title,
            "prop": "pageimages", "format": "json", "pithumbsize": 500
        }
        resp = requests.get(api_url, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        pages = data.get("query", {}).get("pages", {})
        for page_data in pages.values():
            thumb = page_data.get("thumbnail", {})
            if thumb and thumb.get("source"):
                return thumb.get("source")
    except Exception:
        return None
    return None

@st.cache_data(ttl=600)
def fetch_pixabay_images(query, needed=3):
    """Fetch images from Pixabay API"""
    url = "https://pixabay.com/api/"
    params = {
        "key": PIXABAY_API_KEY, "q": query,
        "image_type": "photo", "safesearch": "true", "per_page": needed
    }
    urls = []
    try:
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
        for hit in hits:
            if hit.get("webformatURL"):
                urls.append(hit.get("webformatURL"))
    except Exception:
        pass
    return urls

def gather_images(keyword, serper_images, sources, min_images=3):
    """Gather images from multiple sources"""
    images = []
    for url in serper_images:
        if url not in images:
            images.append(url)
        if len(images) >= min_images:
            return images[:min_images]
    
    for src in sources:
        if "wikipedia.org" in src["link_lower"]:
            wiki_img = fetch_wikipedia_infobox_image(src["link"])
            if wiki_img and wiki_img not in images:
                images.append(wiki_img)
                if len(images) >= min_images:
                    return images[:min_images]
    
    if len(images) < min_images:
        pixabay_imgs = fetch_pixabay_images(keyword, needed=min_images - len(images))
        for u in pixabay_imgs:
            if u and u not in images:
                images.append(u)
                if len(images) >= min_images:
                    break
    
    return images[:min_images]

@st.cache_data(ttl=300)
def search_youtube_videos(query, max_results=3):
    """Search YouTube videos related to the claim"""
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet", "q": query, "type": "video",
        "maxResults": max_results, "key": YOUTUBE_API_KEY
    }
    videos = []
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        for it in items:
            if "videoId" in it["id"]:
                vid = it["id"]["videoId"]
                videos.append({
                    "title": it["snippet"]["title"],
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "thumbnail": it["snippet"]["thumbnails"]["default"]["url"]
                })
    except Exception as e:
        st.warning(f"Could not fetch YouTube videos: {str(e)}")
    return videos

def ask_openai_with_sources_and_claim(claim, sources):
    """Use Azure OpenAI to fact-check the claim based on sources"""
    context = "\n\n".join([f"{s['title']}: {s['snippet']}" for s in sources]) if sources else "No sources available."
    prompt = (
        "Based strictly and only on the sources below, provide a clear, rewritten explanation for the claim "
        "in your own words (do not copy source text or this instruction). "
        "Then, provide a short plain-language answer, and finally output a summary verdict line.\n\n"
        "Format:\nExplanation/Reasoning:\n<your reasoning>\n\nShort Answer:\n<one-sentence answer>\n\nSummary:\nVerdict: REAL|FAKE|MISLEADING\n\n"
        f"Sources:\n{context}\n\nClaim: {claim}"
    )
    try:
        response = client.chat.completions.create(
            model=DEPLOYMENT_NAME,
            messages=[
                {"role": "system", "content": "You are TruthTrace, an AI fact-checking assistant. Always respond factually, neutrally, and in the requested format."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=800,
            temperature=0.2
        )
        content = response.choices[0].message.content.strip()
        verdict_match = re.search(r"Verdict:\s*(REAL|FAKE|MISLEADING)", content, re.IGNORECASE)
        verdict = verdict_match.group(1).upper() if verdict_match else "UNKNOWN"
        return verdict, content
    except Exception as e:
        st.error(f"Failed to get response from Azure OpenAI: {str(e)}")
        return None, None

# Main UI
st.title("🔍 TruthTrace – AI Fact Checker")
st.caption("Powered by Azure OpenAI, Serper, Wikipedia, Pixabay & YouTube Data API")

with st.sidebar:
    st.header("About TruthTrace")
    st.write("TruthTrace uses AI to fact-check claims by:")
    st.write("1. Searching real-time sources via Google")
    st.write("2. Gathering supporting images and videos")
    st.write("3. Analyzing with Azure OpenAI")
    st.write("4. Providing a clear verdict")
    st.divider()
    st.write("**Status:**")
    if SERPER_API_KEY and get_secret("AZURE_OPENAI_KEY") and YOUTUBE_API_KEY:
        st.success("✅ All APIs configured")
    else:
        st.error("⚠️ Missing API keys")

claim = st.text_input("Enter a claim to verify:", placeholder="e.g., Virat Kohli to leave RCB")

if st.button("🔍 Verify Claim", type="primary"):
    if not claim.strip():
        st.error("⚠️ Please enter a claim to verify.")
    else:
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        status_text.text("🔍 Searching for sources...")
        progress_bar.progress(20)
        keyword = extract_main_keyword(claim)
        sources, serper_images = serper_search_with_top_sources_and_images(claim)
        
        if sources:
            progress_bar.progress(40)
            st.subheader("📰 Top Sources")
            for idx, s in enumerate(sources, 1):
                with st.expander(f"Source {idx}: {s['title']}", expanded=(idx==1)):
                    st.markdown(f"**Link:** [{s['link']}]({s['link']})")
                    st.write(s['snippet'])
        else:
            st.info("ℹ️ No relevant sources found.")
        
        status_text.text("🖼️ Gathering related images...")
        progress_bar.progress(60)
        imgs = gather_images(keyword, serper_images, sources)
        if imgs:
            st.subheader("🖼️ Related Images")
            cols = st.columns(min(len(imgs), 3))
            for idx, img_url in enumerate(imgs):
                with cols[idx % 3]:
                    try:
                        st.image(img_url, use_column_width=True)
                    except:
                        pass
        
        status_text.text("🎥 Finding related videos...")
        progress_bar.progress(75)
        vids = search_youtube_videos(keyword)
        if vids:
            st.subheader("🎥 Related YouTube Videos")
            for v in vids:
                col1, col2 = st.columns([1, 4])
                with col1:
                    st.image(v["thumbnail"], width=120)
                with col2:
                    st.markdown(f"**[{v['title']}]({v['url']})**")
        
        status_text.text("🤖 Fact-checking with AI...")
        progress_bar.progress(90)
        verdict, explanation_content = ask_openai_with_sources_and_claim(claim, sources)
        
        progress_bar.progress(100)
        status_text.text("✅ Analysis complete!")
        
        if verdict and explanation_content:
            st.divider()
            st.subheader("📊 Fact-Check Result")
            
            if verdict == "REAL":
                st.success(f"### ✅ Verdict: {verdict}")
            elif verdict == "FAKE":
                st.error(f"### ❌ Verdict: {verdict}")
            elif verdict == "MISLEADING":
                st.warning(f"### ⚠️ Verdict: {verdict}")
            else:
                st.info(f"### ❓ Verdict: {verdict}")
            
            st.markdown("---")
            st.markdown(explanation_content)
        else:
            st.error("❌ Could not complete fact-check. Please try again.")
        
        progress_bar.empty()
        status_text.empty()

st.divider()
st.caption("💡 Tip: Try claims like 'Earth is flat' or 'Water boils at 100°C' or recent news headlines")
