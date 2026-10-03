import asyncio
import json
from bleak import BleakScanner, BleakClient

# Cấu hình UUID hộp thư (Khớp với lib_ble.py trên Pico W)
RX_CHAR_UUID = "00009abc-0000-1000-8000-00805f9b34fb" # Laptop -> Pico W
TX_CHAR_UUID = "00005678-0000-1000-8000-00805f9b34fb" # Pico W -> Laptop

# Giả lập Database và Trạng thái lớp học (Đổi thành "MODE_2" để test báo danh)
GLOBAL_CLASS_MODE = "MODE_3" 
mock_database = {
    "7": {"mssv": "23134024", "verified": True} # Đã xác thực
}

# Tập hợp lưu trữ địa chỉ MAC của các thiết bị ĐANG kết nối để radar không quét trùng
active_connections = set()

# 1. Hàm gửi lệnh từ Laptop -> Pico W
async def send_command_to_device(client: BleakClient, cmd_string: str):
    try:
        payload = json.dumps({"cmd": cmd_string})
        await client.write_gatt_char(RX_CHAR_UUID, payload.encode('utf-8'))
        print(f"[SERVER TX] Đã lệnh cho MAC {client.address} chuyển sang: {cmd_string}")
    except Exception as e:
        print(f"[LỖI GỬI LỆNH] Không thể gửi đến {client.address}: {e}")

# 2. Hàm xử lý dữ liệu từ Pico W -> Laptop
def notification_handler(sender, data):
    try:
        json_string = data.decode('utf-8')
        payload = json.loads(json_string)
        
        dev_id = str(payload.get("dev", "Unknown"))
        
        if "mssv" in payload:
            mssv = payload["mssv"]
            # Khi sinh viên nộp MSSV thành công, cập nhật Database tại đây
            mock_database[dev_id] = {"mssv": mssv, "verified": True}
            print(f"[BÁO DANH] Remote {dev_id} | MSSV: {mssv}")
            
        elif "choice" in payload:
            answers_map = {1: 'A', 2: 'B', 3: 'C', 4: 'D'}
            answer_letter = answers_map.get(payload["choice"], "Lỗi phím")
            print(f"[TRẮC NGHIỆM] Remote {dev_id} | Câu {payload.get('q', 1)}: Chọn {answer_letter}")
        
        elif "key" in payload:
            print(f"[GÕ PHÍM] Remote {dev_id} gõ: {payload['key']}")
            
    except json.JSONDecodeError:
        pass

# 3. Luồng quản lý vòng đời của TỪNG thiết bị (Chạy song song)
async def handle_connection(device):
    mac = device.address
    # Đánh dấu thiết bị này đang được xử lý để Radar bỏ qua
    active_connections.add(mac)
    
    # BÓC TÁCH dev_id TỪ TÊN PHÁT SÓNG (VD: "PicoRemote_7" -> "7")
    name = device.name or "Unknown"
    dev_id = name.split("_")[-1] if "_" in name else "Unknown"
    
    try:
        async with BleakClient(device) as client:
            print(f"\n[+] ĐÃ KẾT NỐI: Remote {dev_id} (MAC: {mac})")
            
            # === THUẬT TOÁN ĐIỀU HƯỚNG TRẠNG THÁI (STATE RECOVERY) ===
            if GLOBAL_CLASS_MODE == "MODE_3":
                if dev_id in mock_database and mock_database[dev_id].get("verified"):
                    print(f" -> Remote {dev_id} đã có phép. Cho phép tiếp tục bài quiz.")
                    await send_command_to_device(client, "MODE_3")
                else:
                    print(f" -> Remote {dev_id} chưa báo danh! Tạm thời khóa ở chế độ chờ (MODE_1).")
                    await send_command_to_device(client, "MODE_1")
                    
            elif GLOBAL_CLASS_MODE == "MODE_2":
                print(f" -> Lớp đang báo danh. Yêu cầu Remote {dev_id} nhập MSSV.")
                await send_command_to_device(client, "MODE_2")
            
            # Mở hòm thư để lắng nghe sinh viên bấm nút
            await client.start_notify(TX_CHAR_UUID, notification_handler)
            
            # Giữ luồng này sống chừng nào thiết bị còn kết nối
            while client.is_connected:
                await asyncio.sleep(1)
                
    except Exception as e:
        # Xảy ra khi mạch bị ngắt điện đột ngột hoặc rớt sóng
        pass
    finally:
        # KHI MẤT KẾT NỐI: Xóa MAC khỏi danh sách để Radar tự động kết nối lại khi có sóng
        active_connections.discard(mac)
        print(f"\n[-] MẤT KẾT NỐI: Remote {dev_id} ({mac}). Hệ thống đang đợi thiết bị online trở lại...")

# 4. Luồng chính: Radar quét liên tục (Continuous Scanner)
async def main():
    print(" 📡 HỆ THỐNG MÁY CHỦ ĐÃ BẬT. Đang dò tìm các mạch PicoRemote...")
    
    while True:
        # Quét không gian trong 2 giây
        devices = await BleakScanner.discover(timeout=2.0)
        
        for d in devices:
            # Chỉ bắt các thiết bị có tiền tố "PicoRemote" VÀ chưa được kết nối
            if d.name and d.name.startswith("PicoRemote"):
                if d.address not in active_connections:
                    print(f" [PHÁT HIỆN] {d.name} ({d.address}). Đang tiến hành móc nối...")
                    # Tạo một Task bất đồng bộ để xử lý thiết bị này, Radar đi quét tiếp
                    asyncio.create_task(handle_connection(d))
                    
        # Nghỉ 0.5s trước khi quét vòng tiếp theo để tránh quá tải card Bluetooth
        await asyncio.sleep(0.5)

if __name__ == "__main__":
    asyncio.run(main())
