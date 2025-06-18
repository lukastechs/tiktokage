import asyncio
import json
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import hashlib
import logging

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import redis.asyncio as redis
from playwright.async_api import async_playwright, Browser, Page
import uvicorn

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="TikTok Account Checker API", version="1.0.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Models
class AccountRequest(BaseModel):
    username: str

class AccountResponse(BaseModel):
    username: str
    display_name: str
    profile_picture: str
    verified: bool
    followers: int
    following: int
    creation_date: Optional[str] = None
    account_age_days: Optional[int] = None
    bio: str = ""
    success: bool = True
    cached: bool = False

class ErrorResponse(BaseModel):
    error: str
    success: bool = False

# Global variables
browser: Optional[Browser] = None
redis_client: Optional[redis.Redis] = None

# Configuration
REDIS_URL = "redis://localhost:6379"  # Change for production
CACHE_EXPIRY = 3600  # 1 hour
REQUEST_DELAY = 2  # seconds between requests
MAX_RETRIES = 3

class TikTokScraper:
    def __init__(self):
        self.browser = None
        self.context = None
    
    async def init_browser(self):
        """Initialize Playwright browser"""
        if self.browser is None:
            playwright = await async_playwright().start()
            self.browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-accelerated-2d-canvas',
                    '--no-first-run',
                    '--no-zygote',
                    '--disable-gpu'
                ]
            )
            self.context = await self.browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
    
    async def close_browser(self):
        """Close browser and context"""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
    
    async def scrape_account(self, username: str) -> Dict[str, Any]:
        """Scrape TikTok account data"""
        await self.init_browser()
        
        # Clean username
        username = username.replace('@', '').strip()
        url = f"https://www.tiktok.com/@{username}"
        
        page = await self.context.new_page()
        
        try:
            # Navigate to profile
            await page.goto(url, wait_until='networkidle', timeout=30000)
            
            # Wait for profile to load
            await page.wait_for_selector('[data-e2e="profile-icon"]', timeout=10000)
            
            # Extract data using multiple selectors as backup
            data = await self._extract_profile_data(page, username)
            
            return data
            
        except Exception as e:
            logger.error(f"Error scraping {username}: {str(e)}")
            raise HTTPException(status_code=404, detail=f"Account not found or private: {username}")
        
        finally:
            await page.close()
    
    async def _extract_profile_data(self, page: Page, username: str) -> Dict[str, Any]:
        """Extract profile data from page"""
        
        # Try to get the __UNIVERSAL_DATA_FOR_REHYDRATION__ script
        try:
            script_content = await page.evaluate("""
                () => {
                    const scripts = document.querySelectorAll('script');
                    for (let script of scripts) {
                        if (script.textContent && script.textContent.includes('__UNIVERSAL_DATA_FOR_REHYDRATION__')) {
                            return script.textContent;
                        }
                    }
                    return null;
                }
            """)
            
            if script_content:
                # Extract JSON data
                json_match = re.search(r'__UNIVERSAL_DATA_FOR_REHYDRATION__\s*=\s*({.*?});', script_content)
                if json_match:
                    data = json.loads(json_match.group(1))
                    return await self._parse_json_data(data, username)
        except Exception as e:
            logger.warning(f"Failed to extract from script tag: {e}")
        
        # Fallback to DOM scraping
        return await self._scrape_dom_data(page, username)
    
    async def _parse_json_data(self, data: Dict, username: str) -> Dict[str, Any]:
        """Parse data from TikTok's JSON structure"""
        try:
            # Navigate through TikTok's data structure
            default_scope = data.get('__DEFAULT_SCOPE__', {})
            webapp_data = default_scope.get('webapp.user-detail', {})
            user_info = webapp_data.get('userInfo', {})
            user = user_info.get('user', {})
            stats = user_info.get('stats', {})
            
            display_name = user.get('nickname', username)
            followers = stats.get('followerCount', 0)
            following = stats.get('followingCount', 0)
            verified = user.get('verified', False)
            avatar = user.get('avatarLarger', user.get('avatarThumb', ''))
            bio = user.get('signature', '')
            
            # Estimate account creation using your advanced logic
            user_id = user.get('id', '')
            total_likes = self._calculate_total_likes(user_info)
            creation_date, age_days = self._estimate_creation_date(
                user_id, username, followers, total_likes, verified
            )
            
            return {
                'username': f"@{username}",
                'display_name': display_name,
                'profile_picture': avatar,
                'verified': verified,
                'followers': followers,
                'following': following,
                'creation_date': creation_date,
                'account_age_days': age_days,
                'bio': bio,
                'success': True
            }
            
        except Exception as e:
            logger.error(f"Error parsing JSON data: {e}")
            raise
    
    async def _scrape_dom_data(self, page: Page, username: str) -> Dict[str, Any]:
        """Fallback DOM scraping method"""
        try:
            # Extract basic profile info
            display_name = await page.text_content('h2[data-e2e="profile-icon"]') or username
            
            # Get follower/following counts
            count_elements = await page.query_selector_all('[data-e2e="followers-count"], [data-e2e="following-count"]')
            
            followers = 0
            following = 0
            
            for element in count_elements:
                text = await element.text_content()
                parent_text = await element.evaluate('el => el.parentElement.textContent')
                
                if 'Follower' in parent_text:
                    followers = self._parse_count(text)
                elif 'Following' in parent_text:
                    following = self._parse_count(text)
            
            # Check verification
            verified = await page.query_selector('[data-e2e="profile-icon-verified"]') is not None
            
            # Get profile picture
            avatar_img = await page.query_selector('img[data-e2e="profile-icon"]')
            avatar = await avatar_img.get_attribute('src') if avatar_img else ''
            
            # Get bio
            bio_element = await page.query_selector('[data-e2e="profile-icon-bio"]')
            bio = await bio_element.text_content() if bio_element else ''
            
            # Estimate creation date using your advanced logic
            total_likes = 0  # Would need to be extracted from page if available
            creation_date, age_days = self._estimate_creation_date(
                '', username, followers, total_likes, verified
            )
            
            return {
                'username': f"@{username}",
                'display_name': display_name,
                'profile_picture': avatar,
                'verified': verified,
                'followers': followers,
                'following': following,
                'creation_date': creation_date,
                'account_age_days': age_days,
                'bio': bio,
                'success': True
            }
            
        except Exception as e:
            logger.error(f"DOM scraping failed: {e}")
            raise
    
    def _parse_count(self, count_str: str) -> int:
        """Parse follower/following count strings like '1.2M', '50.3K'"""
        if not count_str:
            return 0
        
        count_str = count_str.strip().upper()
        
        if 'K' in count_str:
            return int(float(count_str.replace('K', '')) * 1000)
        elif 'M' in count_str:
            return int(float(count_str.replace('M', '')) * 1000000)
        else:
            return int(re.sub(r'[^\d]', '', count_str) or 0)
    
    def _estimate_creation_date(self, user_id: str, username: str, followers: int = 0, total_likes: int = 0, verified: bool = False) -> tuple[str, int]:
        """Estimate account creation date using your advanced logic"""
        estimates = []
        confidence_scores = {"low": 1, "medium": 2, "high": 3}
        
        # Method 1: User ID range estimation
        user_id_estimate = self._estimate_from_user_id(user_id)
        if user_id_estimate:
            estimates.append({
                "date": user_id_estimate,
                "confidence": confidence_scores["high"],
                "method": "User ID Analysis"
            })
        
        # Method 2: Username pattern analysis
        username_estimate = self._estimate_from_username(username)
        if username_estimate:
            estimates.append({
                "date": username_estimate,
                "confidence": confidence_scores["medium"],
                "method": "Username Pattern"
            })
        
        # Method 3: Profile metrics analysis
        metrics_estimate = self._estimate_from_metrics(followers, total_likes, verified)
        if metrics_estimate:
            estimates.append({
                "date": metrics_estimate,
                "confidence": confidence_scores["low"],
                "method": "Profile Metrics"
            })
        
        if not estimates:
            estimated_date = datetime.now()
        else:
            # Weight estimates by confidence and calculate final date
            weighted_sum = sum(est["date"].timestamp() * est["confidence"] for est in estimates)
            total_weight = sum(est["confidence"] for est in estimates)
            final_timestamp = weighted_sum / total_weight
            estimated_date = datetime.fromtimestamp(final_timestamp)
        
        age_days = (datetime.now() - estimated_date).days
        return estimated_date.strftime('%d/%m/%Y'), age_days
    
    def _estimate_from_user_id(self, user_id: str) -> Optional[datetime]:
        """Method 1: User ID range estimation"""
        if not user_id or not user_id.isdigit():
            return None
        
        try:
            uid = int(user_id)
            
            # TikTok user ID ranges (approximate)
            ranges = [
                (0, 100000000, datetime(2016, 9, 1)),  # Early beta
                (100000000, 500000000, datetime(2017, 1, 1)),  # Launch period
                (500000000, 1000000000, datetime(2017, 6, 1)),
                (1000000000, 2000000000, datetime(2018, 1, 1)),
                (2000000000, 5000000000, datetime(2018, 8, 1)),  # Growth period
                (5000000000, 10000000000, datetime(2019, 3, 1)),
                (10000000000, 20000000000, datetime(2019, 9, 1)),
                (20000000000, 50000000000, datetime(2020, 3, 1)),  # COVID boom
                (50000000000, 100000000000, datetime(2020, 9, 1)),
                (100000000000, 200000000000, datetime(2021, 3, 1)),
                (200000000000, 500000000000, datetime(2021, 9, 1)),
                (500000000000, 1000000000000, datetime(2022, 3, 1)),
                (1000000000000, 2000000000000, datetime(2022, 9, 1)),
                (2000000000000, 5000000000000, datetime(2023, 3, 1)),
                (5000000000000, 10000000000000, datetime(2023, 9, 1)),
                (10000000000000, 20000000000000, datetime(2024, 3, 1)),
                (20000000000000, float('inf'), datetime(2024, 9, 1))
            ]
            
            for min_id, max_id, date in ranges:
                if min_id <= uid < max_id:
                    return date
            
            return datetime.now()
        except (ValueError, OverflowError):
            return None
    
    def _estimate_from_username(self, username: str) -> Optional[datetime]:
        """Method 2: Username pattern analysis"""
        if not username:
            return None
        
        import re
        
        # Early TikTok usernames had different patterns
        patterns = [
            (re.compile(r'^user\d{7,9}

# Initialize scraper
scraper = TikTokScraper()

@app.on_event("startup")
async def startup_event():
    """Initialize Redis connection"""
    global redis_client
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info("Redis connection established")
    except Exception as e:
        logger.warning(f"Redis connection failed: {e}")
        redis_client = None

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup resources"""
    await scraper.close_browser()
    if redis_client:
        await redis_client.close()

async def get_cache_key(username: str) -> str:
    """Generate cache key for username"""
    return f"tiktok:{username.lower()}"

async def get_cached_data(username: str) -> Optional[Dict]:
    """Get cached account data"""
    if not redis_client:
        return None
    
    try:
        cache_key = await get_cache_key(username)
        cached = await redis_client.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.error(f"Cache read error: {e}")
    
    return None

async def cache_data(username: str, data: Dict):
    """Cache account data"""
    if not redis_client:
        return
    
    try:
        cache_key = await get_cache_key(username)
        await redis_client.setex(cache_key, CACHE_EXPIRY, json.dumps(data))
    except Exception as e:
        logger.error(f"Cache write error: {e}")

@app.get("/")
async def root():
    """Health check endpoint"""
    return {"message": "TikTok Account Checker API", "status": "running"}

@app.post("/check-account", response_model=AccountResponse)
async def check_account(request: AccountRequest, background_tasks: BackgroundTasks):
    """Check TikTok account information"""
    username = request.username.replace('@', '').strip()
    
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    
    # Check cache first
    cached_data = await get_cached_data(username)
    if cached_data:
        cached_data['cached'] = True
        return AccountResponse(**cached_data)
    
    # Rate limiting delay
    await asyncio.sleep(REQUEST_DELAY)
    
    # Scrape data
    try:
        data = await scraper.scrape_account(username)
        
        # Cache the result in background
        background_tasks.add_task(cache_data, username, data)
        
        return AccountResponse(**data)
        
    except Exception as e:
        logger.error(f"Error checking account {username}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cache-stats")
async def cache_stats():
    """Get cache statistics"""
    if not redis_client:
        return {"cache": "disabled"}
    
    try:
        info = await redis_client.info('memory')
        keys = await redis_client.dbsize()
        return {
            "cache": "enabled",
            "keys": keys,
            "memory_used": info.get('used_memory_human', 'N/A')
        }
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)), datetime(2016, 9, 1)),  # user1234567
            (re.compile(r'^[a-z]{3,8}\d{2,4}

# Initialize scraper
scraper = TikTokScraper()

@app.on_event("startup")
async def startup_event():
    """Initialize Redis connection"""
    global redis_client
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info("Redis connection established")
    except Exception as e:
        logger.warning(f"Redis connection failed: {e}")
        redis_client = None

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup resources"""
    await scraper.close_browser()
    if redis_client:
        await redis_client.close()

async def get_cache_key(username: str) -> str:
    """Generate cache key for username"""
    return f"tiktok:{username.lower()}"

async def get_cached_data(username: str) -> Optional[Dict]:
    """Get cached account data"""
    if not redis_client:
        return None
    
    try:
        cache_key = await get_cache_key(username)
        cached = await redis_client.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.error(f"Cache read error: {e}")
    
    return None

async def cache_data(username: str, data: Dict):
    """Cache account data"""
    if not redis_client:
        return
    
    try:
        cache_key = await get_cache_key(username)
        await redis_client.setex(cache_key, CACHE_EXPIRY, json.dumps(data))
    except Exception as e:
        logger.error(f"Cache write error: {e}")

@app.get("/")
async def root():
    """Health check endpoint"""
    return {"message": "TikTok Account Checker API", "status": "running"}

@app.post("/check-account", response_model=AccountResponse)
async def check_account(request: AccountRequest, background_tasks: BackgroundTasks):
    """Check TikTok account information"""
    username = request.username.replace('@', '').strip()
    
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    
    # Check cache first
    cached_data = await get_cached_data(username)
    if cached_data:
        cached_data['cached'] = True
        return AccountResponse(**cached_data)
    
    # Rate limiting delay
    await asyncio.sleep(REQUEST_DELAY)
    
    # Scrape data
    try:
        data = await scraper.scrape_account(username)
        
        # Cache the result in background
        background_tasks.add_task(cache_data, username, data)
        
        return AccountResponse(**data)
        
    except Exception as e:
        logger.error(f"Error checking account {username}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cache-stats")
async def cache_stats():
    """Get cache statistics"""
    if not redis_client:
        return {"cache": "disabled"}
    
    try:
        info = await redis_client.info('memory')
        keys = await redis_client.dbsize()
        return {
            "cache": "enabled",
            "keys": keys,
            "memory_used": info.get('used_memory_human', 'N/A')
        }
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)), datetime(2017, 3, 1)),  # abc123
            (re.compile(r'^\w{3,8}

# Initialize scraper
scraper = TikTokScraper()

@app.on_event("startup")
async def startup_event():
    """Initialize Redis connection"""
    global redis_client
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info("Redis connection established")
    except Exception as e:
        logger.warning(f"Redis connection failed: {e}")
        redis_client = None

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup resources"""
    await scraper.close_browser()
    if redis_client:
        await redis_client.close()

async def get_cache_key(username: str) -> str:
    """Generate cache key for username"""
    return f"tiktok:{username.lower()}"

async def get_cached_data(username: str) -> Optional[Dict]:
    """Get cached account data"""
    if not redis_client:
        return None
    
    try:
        cache_key = await get_cache_key(username)
        cached = await redis_client.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.error(f"Cache read error: {e}")
    
    return None

async def cache_data(username: str, data: Dict):
    """Cache account data"""
    if not redis_client:
        return
    
    try:
        cache_key = await get_cache_key(username)
        await redis_client.setex(cache_key, CACHE_EXPIRY, json.dumps(data))
    except Exception as e:
        logger.error(f"Cache write error: {e}")

@app.get("/")
async def root():
    """Health check endpoint"""
    return {"message": "TikTok Account Checker API", "status": "running"}

@app.post("/check-account", response_model=AccountResponse)
async def check_account(request: AccountRequest, background_tasks: BackgroundTasks):
    """Check TikTok account information"""
    username = request.username.replace('@', '').strip()
    
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    
    # Check cache first
    cached_data = await get_cached_data(username)
    if cached_data:
        cached_data['cached'] = True
        return AccountResponse(**cached_data)
    
    # Rate limiting delay
    await asyncio.sleep(REQUEST_DELAY)
    
    # Scrape data
    try:
        data = await scraper.scrape_account(username)
        
        # Cache the result in background
        background_tasks.add_task(cache_data, username, data)
        
        return AccountResponse(**data)
        
    except Exception as e:
        logger.error(f"Error checking account {username}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cache-stats")
async def cache_stats():
    """Get cache statistics"""
    if not redis_client:
        return {"cache": "disabled"}
    
    try:
        info = await redis_client.info('memory')
        keys = await redis_client.dbsize()
        return {
            "cache": "enabled",
            "keys": keys,
            "memory_used": info.get('used_memory_human', 'N/A')
        }
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)), datetime(2017, 9, 1)),  # simple names
            (re.compile(r'^.{1,8}

# Initialize scraper
scraper = TikTokScraper()

@app.on_event("startup")
async def startup_event():
    """Initialize Redis connection"""
    global redis_client
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info("Redis connection established")
    except Exception as e:
        logger.warning(f"Redis connection failed: {e}")
        redis_client = None

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup resources"""
    await scraper.close_browser()
    if redis_client:
        await redis_client.close()

async def get_cache_key(username: str) -> str:
    """Generate cache key for username"""
    return f"tiktok:{username.lower()}"

async def get_cached_data(username: str) -> Optional[Dict]:
    """Get cached account data"""
    if not redis_client:
        return None
    
    try:
        cache_key = await get_cache_key(username)
        cached = await redis_client.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.error(f"Cache read error: {e}")
    
    return None

async def cache_data(username: str, data: Dict):
    """Cache account data"""
    if not redis_client:
        return
    
    try:
        cache_key = await get_cache_key(username)
        await redis_client.setex(cache_key, CACHE_EXPIRY, json.dumps(data))
    except Exception as e:
        logger.error(f"Cache write error: {e}")

@app.get("/")
async def root():
    """Health check endpoint"""
    return {"message": "TikTok Account Checker API", "status": "running"}

@app.post("/check-account", response_model=AccountResponse)
async def check_account(request: AccountRequest, background_tasks: BackgroundTasks):
    """Check TikTok account information"""
    username = request.username.replace('@', '').strip()
    
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    
    # Check cache first
    cached_data = await get_cached_data(username)
    if cached_data:
        cached_data['cached'] = True
        return AccountResponse(**cached_data)
    
    # Rate limiting delay
    await asyncio.sleep(REQUEST_DELAY)
    
    # Scrape data
    try:
        data = await scraper.scrape_account(username)
        
        # Cache the result in background
        background_tasks.add_task(cache_data, username, data)
        
        return AccountResponse(**data)
        
    except Exception as e:
        logger.error(f"Error checking account {username}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cache-stats")
async def cache_stats():
    """Get cache statistics"""
    if not redis_client:
        return {"cache": "disabled"}
    
    try:
        info = await redis_client.info('memory')
        keys = await redis_client.dbsize()
        return {
            "cache": "enabled",
            "keys": keys,
            "memory_used": info.get('used_memory_human', 'N/A')
        }
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)), datetime(2018, 6, 1)),  # very short names
        ]
        
        clean_username = username.replace('@', '')
        
        for pattern, date in patterns:
            if pattern.match(clean_username):
                return date
        
        return None
    
    def _estimate_from_metrics(self, followers: int, total_likes: int, verified: bool) -> Optional[datetime]:
        """Method 3: Profile metrics analysis"""
        scores = []
        
        # High follower count suggests older account
        if followers > 1000000:
            scores.append(datetime(2018, 1, 1))
        elif followers > 100000:
            scores.append(datetime(2019, 1, 1))
        elif followers > 10000:
            scores.append(datetime(2020, 1, 1))
        else:
            scores.append(datetime(2021, 1, 1))
        
        # Very high likes suggest established account
        if total_likes > 10000000:
            scores.append(datetime(2018, 6, 1))
        elif total_likes > 1000000:
            scores.append(datetime(2019, 6, 1))
        elif total_likes > 100000:
            scores.append(datetime(2020, 6, 1))
        
        # Verified accounts are typically older
        if verified:
            scores.append(datetime(2018, 1, 1))
        
        if not scores:
            return None
        
        # Return the earliest date from scores
        return min(scores)
    
    def _calculate_total_likes(self, user_data: Dict) -> int:
        """Extract total likes from user data if available"""
        # This would need to be implemented based on the actual data structure
        # For now, return 0 as placeholder
        return 0

# Initialize scraper
scraper = TikTokScraper()

@app.on_event("startup")
async def startup_event():
    """Initialize Redis connection"""
    global redis_client
    try:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info("Redis connection established")
    except Exception as e:
        logger.warning(f"Redis connection failed: {e}")
        redis_client = None

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup resources"""
    await scraper.close_browser()
    if redis_client:
        await redis_client.close()

async def get_cache_key(username: str) -> str:
    """Generate cache key for username"""
    return f"tiktok:{username.lower()}"

async def get_cached_data(username: str) -> Optional[Dict]:
    """Get cached account data"""
    if not redis_client:
        return None
    
    try:
        cache_key = await get_cache_key(username)
        cached = await redis_client.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        logger.error(f"Cache read error: {e}")
    
    return None

async def cache_data(username: str, data: Dict):
    """Cache account data"""
    if not redis_client:
        return
    
    try:
        cache_key = await get_cache_key(username)
        await redis_client.setex(cache_key, CACHE_EXPIRY, json.dumps(data))
    except Exception as e:
        logger.error(f"Cache write error: {e}")

@app.get("/")
async def root():
    """Health check endpoint"""
    return {"message": "TikTok Account Checker API", "status": "running"}

@app.post("/check-account", response_model=AccountResponse)
async def check_account(request: AccountRequest, background_tasks: BackgroundTasks):
    """Check TikTok account information"""
    username = request.username.replace('@', '').strip()
    
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    
    # Check cache first
    cached_data = await get_cached_data(username)
    if cached_data:
        cached_data['cached'] = True
        return AccountResponse(**cached_data)
    
    # Rate limiting delay
    await asyncio.sleep(REQUEST_DELAY)
    
    # Scrape data
    try:
        data = await scraper.scrape_account(username)
        
        # Cache the result in background
        background_tasks.add_task(cache_data, username, data)
        
        return AccountResponse(**data)
        
    except Exception as e:
        logger.error(f"Error checking account {username}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cache-stats")
async def cache_stats():
    """Get cache statistics"""
    if not redis_client:
        return {"cache": "disabled"}
    
    try:
        info = await redis_client.info('memory')
        keys = await redis_client.dbsize()
        return {
            "cache": "enabled",
            "keys": keys,
            "memory_used": info.get('used_memory_human', 'N/A')
        }
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
