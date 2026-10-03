import asyncio
import json
from bleak import BleakScanner, BleakClient

DEVICE_NAME = "PicoRemote"
CHAR_UUID = "00005678-0000-1000-8000-00805f9b34fb"

# Hàm bóc tách và phân loại dữ liệu JSON từ Pico W
def notification_handler(sender, data):
    try:
        # 1. Giải mã bytearray thành chuỗi và parse thành Dictionary
        json_string = data.decode('utf-8')
        payload = json.loads(json_string)
        
        dev_id = payload.get("dev", "Unknown")
        seq = payload.get("seq", 0)
        
        # 2. Phân loại gói tin dựa trên Key có trong JSON
        if "mssv" in payload:
            mssv = payload["mssv"]
            hw = payload.get("hw", "unknown")
            print(f"[BÁO DANH] Remote {dev_id} | MSSV: {mssv} | Phần cứng: {hw} | Seq: {seq}")
            
            # Gợi ý: Gửi API đánh dấu sinh viên có mặt lên Web Server tại đây
            
        elif "choice" in payload:
            choice = payload["choice"]
            question = payload.get("q", 1)
            
            # Chuyển đổi số thành chữ cái đáp án cho dễ nhìn
            answers_map = {1: 'A', 2: 'B', 3: 'C', 4: 'D'}
            answer_letter = answers_map.get(choice, "Lỗi phím")
            
            print(f"[TRẮC NGHIỆM] Remote {dev_id} | Câu {question}: Chọn {answer_letter} | Seq: {seq}")
            
            # Gợi ý: Bắn dữ liệu qua WebSocket để biểu đồ trên Web nhảy số tại đây
        
        elif "key" in payload:
            key = payload["key"]
            print(f"[GÕ PHÍM] Remote {dev_id} vừa gõ phím: {key}")
        
        else:
            print(f"[KHÔNG XÁC ĐỊNH] Gói tin lạ: {payload}")
            
    except json.JSONDecodeError:
        print(f"[LỖI FORMAT] Dữ liệu không phải chuẩn JSON: {data.decode('utf-8')}")

async def main():
    print(f" Đang rà quét không gian để tìm '{DEVICE_NAME}'...")
    device = await BleakScanner.find_device_by_name(DEVICE_NAME)

    if device is None:
        print(f" Không tìm thấy '{DEVICE_NAME}'.")
        return

    print(f" Đã khóa mục tiêu: {device.name} [{device.address}]")

    async with BleakClient(device) as client:
        print(" Kết nối thành công! Đèn LED ở GP4 trên Pico W sẽ sáng lên.")
        await client.start_notify(CHAR_UUID, notification_handler)
        
        print("\n Hệ thống đã sẵn sàng rà quét JSON. Hãy bấm phím trên mạch!")
        print(" (Nhấn Ctrl+C để thoát)\n")
        
        while True:
            await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())