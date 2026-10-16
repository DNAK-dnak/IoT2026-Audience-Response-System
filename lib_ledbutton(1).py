from machine import Pin
import time

class LEDButton:
# Thêm button_value vào tham số khởi tạo
    def __init__(self, button_pin, button_value, callback_func, debounce_ms=200):
        self.button_pin = button_pin
        self.button_value = button_value  # Lưu lại giá trị đại diện của nút
        self.button = Pin(button_pin, Pin.IN, Pin.PULL_UP)
        self.debounce_ms = debounce_ms
        self.last_press_time = 0
        self.callback_func = callback_func
            
        self.button.irq(trigger=Pin.IRQ_FALLING, handler=self.button_handler)

    def button_handler(self, pin):
        current_time = time.ticks_ms()
        if time.ticks_diff(current_time, self.last_press_time) > self.debounce_ms:
            self.last_press_time = current_time
            # Trả về cho hàm chính CẢ số chân pin VÀ giá trị phím
            self.callback_func(self.button_pin, self.button_value)

