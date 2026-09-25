# TPMSTempNet

## Instructions for feeding WeeWX's existing StdRESTful configurations

* Install https://github.com/merbanan/rtl_433 & https://github.com/weewx/weewx

### In `weewx/weewx-data/weewx.conf`: 

* Change `station_type = Simulator` to `station_type = SDR`

* Add SDR block above the Simulator block, eg.

```
[SDR]
    driver = user.sdr
    cmd = /usr/local/bin/rtl_433 -M utc -F json -f 315M -s 1024k -g 42.1
    [[sensor_map]]
        extraTemp1 = temperature.00000000.ToyotaTPMSPacket
        extraTemp2 = temperature.00000000.ToyotaTPMSPacket
        extraTemp3 = temperature.00000000.ToyotaTPMSPacket
        extraTemp4 = temperature.00000000.ToyotaTPMSPacket
        extraTemp5 = temperature.00000000.Elantra2012TPMSPacket
        extraTemp6 = temperature.00000000.Elantra2012TPMSPacket
```

... replacing the 0s with any nearby TPMS IDs you've decoded (that aren't printing insane false positives like hundreds of degrees, or below -40 degrees, where no TPMS should actually be able to operate?)

Regardless of you saving them in your configuration, NO TPMS ID IS NOR EVEN CAN BE UPLOADED - only decoded temperatures.

If you've decoded sane TPMS on multiple frequencies, you may set them next to each other with a frequency hopper 
eg. `-H 5 -f 315M -f 433.92M`

* Add `user.mintemp.MinTempService` to `data_services = ` under the Engine Services (or if you already added something after `data_services = ` then put it after a comma & space: `, user.mintemp.MinTempService` as the end of the line, no other comma or space). If nothing else, it should be `data_services = user.mintemp.MinTempService` between the `prep_services` & `process_services` lines; don't move it anywhere else.

### Then...

* Save the .py scripts in `weewx/weewx-data/bin/user/`

* Add any TPMS class not already present into `sdr.py` (Toyota & Elantra2012 are there in alphabetical order)

* Set sane bounds for your local climate (using Celsius, NOT Fahrenheit) in `mintemp.py` (originally set for San Diego, CA)

And run: `./weewxd weewx/weewx-data/weewx.conf`