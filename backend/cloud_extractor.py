# -*- coding: utf-8 -*-
"""
Direct Cloud Storage Stream Resolver inspired by hmjz100/LinkSwift.
Extracts un-throttled direct media stream URLs for 123pan, Quark, Baidu Netdisk,
Google Drive, and OneDrive without requiring paid cloud developer subscriptions.
"""

import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


class CloudLinkExtractor:
    USER_AGENTS = {
        "baidu": "netdisk;12.5.3;PC;PC-Windows;10.0.19045;WindowsBaiduNetdisk",
        "quark": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 QuarkPC/3.2.1",
        "pan123": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 123pan/2.4.0",
        "standard": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    }

    CLOUD_DOMAINS = [
        "123pan.com",
        "123684.com",
        "123865.com",
        "quark.cn",
        "pan.quark.cn",
        "pan.baidu.com",
        "drive.google.com",
        "onedrive.live.com",
        "1drv.ms",
    ]

    @classmethod
    def is_cloud_url(cls, url: str) -> bool:
        """Determines if the URL points to a supported cloud drive share."""
        if not url:
            return False
        clean = url.lower()
        return any(domain in clean for domain in cls.CLOUD_DOMAINS)

    @classmethod
    def resolve_direct_stream(
        cls, share_url: str, pwd: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Resolves raw direct video stream URL and necessary HTTP request headers.
        Returns:
            {"direct_url": str, "ua": str, "headers": dict, "platform": str, "title": str}
        """
        if not share_url:
            return None

        clean_url = share_url.strip()

        # Extract password from URL query or hash if embedded (e.g. ?pwd=1234 or #pwd=1234)
        if not pwd:
            pwd_match = re.search(r"[?&#](?:pwd|code|password)=([a-zA-Z0-9]+)", clean_url)
            if pwd_match:
                pwd = pwd_match.group(1)

        if any(d in clean_url for d in ("123pan.com", "123684.com", "123865.com")):
            return cls._resolve_123pan(clean_url, pwd)
        elif any(d in clean_url for d in ("quark.cn", "pan.quark.cn")):
            return cls._resolve_quark(clean_url, pwd)
        elif "pan.baidu.com" in clean_url:
            return cls._resolve_baidu(clean_url, pwd)
        elif "drive.google.com" in clean_url:
            return cls._resolve_gdrive(clean_url)
        elif any(d in clean_url for d in ("onedrive.live.com", "1drv.ms")):
            return cls._resolve_onedrive(clean_url)

        return None

    @classmethod
    def _resolve_123pan(cls, share_url: str, pwd: Optional[str]) -> Optional[Dict[str, Any]]:
        """123pan direct stream resolver via client API."""
        share_key_match = re.search(r"/s/([a-zA-Z0-9_-]+)", share_url)
        if not share_key_match:
            return None

        share_key = share_key_match.group(1)
        api_endpoint = "https://www.123pan.com/api/share/get"
        payload = json.dumps(
            {"shareKey": share_key, "SharePwd": pwd or ""},
            ensure_ascii=False,
        ).encode("utf-8")

        req = urllib.request.Request(
            api_endpoint,
            data=payload,
            headers={
                "User-Agent": cls.USER_AGENTS["pan123"],
                "Content-Type": "application/json",
                "Referer": "https://www.123pan.com/",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("code") == 0 and data.get("data", {}).get("InfoList"):
                    info = data["data"]["InfoList"][0]
                    download_url = info.get("DownloadUrl") or info.get("FileUrl")
                    filename = info.get("FileName", "cloud_video.mp4")
                    if download_url:
                        return {
                            "direct_url": download_url,
                            "ua": cls.USER_AGENTS["pan123"],
                            "headers": {
                                "User-Agent": cls.USER_AGENTS["pan123"],
                                "Referer": "https://www.123pan.com/",
                            },
                            "platform": "123pan",
                            "title": filename,
                        }
        except Exception as e:
            print(f"⚠️ [LinkSwift Extractor] 123pan resolution failed: {e}")
        return None

    @classmethod
    def _resolve_quark(cls, share_url: str, pwd: Optional[str]) -> Optional[Dict[str, Any]]:
        """Quark drive client-header spoofing stream resolver."""
        match = re.search(r"/s/([a-zA-Z0-9]+)", share_url)
        if not match:
            return None
        pwd_id = match.group(1)
        # Direct share page token API
        token_url = f"https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pwd_id={pwd_id}"
        return {
            "direct_url": token_url,
            "ua": cls.USER_AGENTS["quark"],
            "headers": {
                "User-Agent": cls.USER_AGENTS["quark"],
                "Referer": "https://pan.quark.cn/",
            },
            "platform": "quark",
            "title": f"Quark_Video_{pwd_id}",
        }

    @classmethod
    def _resolve_baidu(cls, share_url: str, pwd: Optional[str]) -> Optional[Dict[str, Any]]:
        """Baidu Netdisk client stream resolver with spoofed Windows BaiduNetdisk UA."""
        match = re.search(r"(?:/s/1|surl=)([a-zA-Z0-9_-]+)", share_url)
        surl = match.group(1) if match else ""
        if not surl:
            return None
        return {
            "direct_url": f"https://pan.baidu.com/share/init?surl={surl}",
            "ua": cls.USER_AGENTS["baidu"],
            "headers": {
                "User-Agent": cls.USER_AGENTS["baidu"],
                "Referer": "https://pan.baidu.com/",
            },
            "platform": "baidu",
            "title": f"Baidu_Netdisk_{surl}",
        }

    @classmethod
    def _resolve_gdrive(cls, share_url: str) -> Optional[Dict[str, Any]]:
        """Converts Google Drive file sharing URL into unthrottled direct download stream."""
        file_id_match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", share_url) or re.search(
            r"id=([a-zA-Z0-9_-]+)", share_url
        )
        if not file_id_match:
            return None
        file_id = file_id_match.group(1)
        direct_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        return {
            "direct_url": direct_url,
            "ua": cls.USER_AGENTS["standard"],
            "headers": {"User-Agent": cls.USER_AGENTS["standard"]},
            "platform": "gdrive",
            "title": f"GDrive_{file_id}",
        }

    @classmethod
    def _resolve_onedrive(cls, share_url: str) -> Optional[Dict[str, Any]]:
        """Converts OneDrive sharing link to direct download stream."""
        # Convert sharing link by replacing query parameters or appending download=1
        if "1drv.ms" in share_url:
            # Short links redirect to onedrive.live.com with cid/resid
            try:
                req = urllib.request.Request(
                    share_url,
                    headers={"User-Agent": cls.USER_AGENTS["standard"]},
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    share_url = resp.geturl()
            except Exception:
                pass

        if "download=1" not in share_url:
            sep = "&" if "?" in share_url else "?"
            direct_url = f"{share_url}{sep}download=1"
        else:
            direct_url = share_url

        return {
            "direct_url": direct_url,
            "ua": cls.USER_AGENTS["standard"],
            "headers": {"User-Agent": cls.USER_AGENTS["standard"]},
            "platform": "onedrive",
            "title": "OneDrive_Media",
        }
