import argparse
import json
import os
import sys
import uvicorn

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LTstudio Studio Server & Agent-Reach Harvester")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8090")), help="Server port (default: 8090)")
    parser.add_argument("--harvest", type=str, default="", help="Harvest viral synopsis & script from topic/query")
    parser.add_argument("--lang", type=str, default="Burmese (Myanmar Language)", help="Target language for script")
    parser.add_argument("--duration", type=float, default=60.0, help="Target duration in seconds")
    parser.add_argument("--voice", type=str, default="Charon", help="Voice persona")

    args = parser.parse_args()

    if args.harvest:
        from backend.script_harvester import harvest_trending_script, synthesize_recap_script
        print("=" * 60)
        print(f"🌾 LTstudio Agent-Reach Harvester: '{args.harvest}'")
        print("=" * 60)
        harvest_res = harvest_trending_script(args.harvest)
        print(f"Sources Found: {harvest_res.get('sources_count', 0)} ({harvest_res.get('harvester')})")
        for s in harvest_res.get("sources", []):
            print(f"  • [{s.get('platform')}] {s.get('title')}")
            if s.get("url"):
                print(f"    URL: {s.get('url')}")

        print("\n🧠 Synthesizing Syllable-Timed Viral Script...")
        script = synthesize_recap_script(
            harvest_res,
            duration_sec=args.duration,
            target_language=args.lang,
            voice=args.voice,
        )
        print("\n🎬 Viral Title:", script.get("viral_title"))
        print("🎣 Hook Line  :", script.get("hook_line"))
        print("\n📝 Narration Script:")
        print(script.get("full_narration"))
        print(f"\n⏱ Captions ({len(script.get('captions', []))} subtitle lines generated):")
        for c in script.get("captions", [])[:5]:
            print(f"  [{c.get('start'):.1f}s - {c.get('end'):.1f}s]: {c.get('text')}")
        print("=" * 60)
        sys.exit(0)

    from backend.app import app
    print(f"Starting LTstudio Studio at http://127.0.0.1:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port)
