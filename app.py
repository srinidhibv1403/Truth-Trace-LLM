import streamlit as st
from openai import AzureOpenAI
import requests
import re
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Azure OpenAI Configuration
client = AzureOpenAI(
    api_key=os.getenv("AZURE_OPENAI_KEY"),
    api_version="2024-02-15-preview",
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT")
)

DEPLOYMENT_NAME = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4")
SERPER_API_KEY = os.getenv("SERPER_API_KEY")
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")

def extract_main_keyword(claim):
    splitters = [' who ', ' that ', ' which ', ' is ', ' has ',
                 ' was ', ' were ', ' won ', ' scored ', ' can ']
    cl = claim.lower()
    for s in splitters:
        if s in cl:
            return claim[:cl.index(s)].strip()
    return claim.strip()

def serper_search_with_top_sources_and_images(query, top_n=4):
    url = "https://google.serper.dev/search"
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    sources, images = [], []
    try:
        resp = requests.post(url, headers=headers,
                             json={"q": query, "gl": "us", "hl": "en"})
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
            for img in data["images"]:
                img_url = img.get("thumbnail") or img.get("url")
                if img_url:
                    images.append(img_url)
    except Exception as e:
        st.error(f"Failed to fetch search results from Serper: {e}")
    return sources, images

def fetch_wikipedia_infobox_image(wikipedia_url):
    try:
        title = wikipedia_url.split("/wiki/")[-1].replace("_", " ")
        api_url = "https://en.wikipedia.org/w/api.php"
        params = {
            "action": "query", "titles": title,
            "prop": "pageimages", "format": "json", "pithumbsize": 500
        }
        resp = requests.get(api_url, params=params)
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

def fetch_pixabay_images(query, needed=3):
    url = "https://pixabay.com/api/"
    params = {
        "key": PIXABAY_API_KEY, "q": query,
        "image_type": "photo", "safesearch": "true", "per_page": needed
    }
    urls = []
    try:
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
        for hit in hits:
            if hit.get("webformatURL"):
                urls.append(hit.get("webformatURL"))
    except Exception:
        pass
    return urls

def gather_images(keyword, serper_images, sources, min_images=3):
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

def search_youtube_videos(query, max_results=3):
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet", "q": query, "type": "video",
        "maxResults": max_results, "key": YOUTUBE_API_KEY
    }
    videos = []
    try:
        resp = requests.get(url, params=params)
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
        st.error(f"Failed to fetch YouTube videos: {e}")
    return videos

def ask_openai_with_sources_and_claim(claim, sources):
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
        st.error(f"Failed to get response from Azure OpenAI: {e}")
        return None, None

st.set_page_config(page_title="TruthTrace Fact Checker", page_icon="🔍")
st.title("TruthTrace – AI Fact Checker with Sources, Images, Videos & Summary")
st.caption("Powered by Serper, Wikipedia, Pixabay, YouTube Data API & Azure OpenAI")

claim = st.text_input("Enter a claim to verify:")

if st.button("Verify"):
    if not claim.strip():
        st.error("Please enter a claim to verify.")
    else:
        keyword = extract_main_keyword(claim)
        st.write("🔍 Searching sources...")
        sources, serper_images = serper_search_with_top_sources_and_images(claim)
        
        if sources:
            st.subheader("Top 4 Sources")
            for s in sources:
                st.markdown(f"- [{s['title']}]({s['link']})")
                st.write(s['snippet'])
        else:
            st.info("No relevant sources found.")
        
        imgs = gather_images(keyword, serper_images, sources)
        if imgs:
            st.subheader("Related Images")
            cols = st.columns(3)
            for idx, img_url in enumerate(imgs):
                with cols[idx % 3]:
                    st.image(img_url, width=200)
        else:
            st.info("No related images found.")
        
        vids = search_youtube_videos(keyword)
        st.subheader("Related YouTube Videos")
        if vids:
            for v in vids:
                col1, col2 = st.columns([1, 3])
                with col1:
                    st.image(v["thumbnail"], width=120)
                with col2:
                    st.markdown(f"[{v['title']}]({v['url']})")
        else:
            st.info("No related videos found.")
        
        st.write("🔍 Fact-checking claim...")
        verdict, explanation_content = ask_openai_with_sources_and_claim(claim, sources)
        
        if verdict and explanation_content:
            # Color-code the verdict
            if verdict == "REAL":
                st.success(f"**Final Verdict:** ✅ {verdict}")
            elif verdict == "FAKE":
                st.error(f"**Final Verdict:** ❌ {verdict}")
            else:
                st.warning(f"**Final Verdict:** ⚠️ {verdict}")
            
            st.write(explanation_content)
        else:
            st.error("Could not get fact-check result.")
