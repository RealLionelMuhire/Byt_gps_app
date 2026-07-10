import psycopg2
import sys
import os

def get_db_url():
    # Try to get from environment first
    url = os.environ.get("DATABASE_URL")
    if url: return url
    
    # Try to read from server/.env
    env_path = os.path.join(os.path.dirname(__file__), "server", ".env")
    try:
        with open(env_path, "r") as f:
            for line in f:
                if line.startswith("DATABASE_URL="):
                    return line.strip().split("=", 1)[1].strip("'\"")
    except FileNotFoundError:
        pass
    
    print("❌ Error: Could not find DATABASE_URL in environment or server/.env")
    sys.exit(1)

DATABASE_URL = get_db_url()

def main():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        
        # 1. Check devices
        cur.execute("SELECT id, imei, name, status, last_update FROM devices ORDER BY id ASC;")
        devices = cur.fetchall()
        print("=== DEVICES ===")
        for d in devices:
            print(f"ID: {d[0]:<3} | IMEI: {d[1]:<16} | Name: {d[2]:<15} | Status: {d[3]:<10} | Last Update: {d[4]}")
            
        print("\n=== LATEST LOCATIONS ===")
        for d in devices:
            dev_id = d[0]
            cur.execute("SELECT timestamp, latitude, longitude, speed, satellites, gps_valid FROM locations WHERE device_id = %s ORDER BY timestamp DESC LIMIT 3;", (dev_id,))
            locs = cur.fetchall()
            print(f"Device {dev_id} ({d[1]}) - Last 3 Locations:")
            if not locs:
                print("  No locations found!")
            for l in locs:
                print(f"  {l[0]} | Lat: {l[1]}, Lon: {l[2]} | Speed: {l[3]} | Sats: {l[4]} | Valid: {l[5]}")
                
        print("\n=== TRIPS ===")
        for d in devices:
            dev_id = d[0]
            cur.execute("SELECT id, start_time, end_time, total_distance_km, name FROM trips WHERE device_id = %s ORDER BY start_time DESC LIMIT 3;", (dev_id,))
            trips = cur.fetchall()
            print(f"Device {dev_id} ({d[1]}) - Last 3 Trips:")
            if not trips:
                print("  No trips found!")
            for t in trips:
                print(f"  ID: {t[0]} | Start: {t[1]} | End: {t[2]} | Dist: {t[3]}km | Name: {t[4]}")
                
    except Exception as e:
        print(f"Error: {e}")
    finally:
        if 'conn' in locals() and conn:
            cur.close()
            conn.close()

if __name__ == "__main__":
    main()
