import adafruit_dht
import time
import board

DHT_sensor = adafruit_dht.DHT11(board.D2)

for i in range(10):
#while True:
	try:
		temperature = DHT_sensor.temperature
		humidity = DHT_sensor.humidity
		if humidity is not None and temperature is not None:
			print(f"Room Temperature ={temperature} Degree Celsius Humidity ={humidity} %")
		else:
			print ("Sensor failure, check wiring.");
	except Exception as e:
		print("something went wrong :(")

	time.sleep(2);
