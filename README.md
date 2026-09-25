# TPMSTempNet

## Instructions for feeding WeeWX's existing StdRESTful configurations

* Install https://github.com/merbanan/rtl_433 & https://github.com/weewx/weewx

* Run `rtl_433` separately for a site survey, logging TPMS decodes over as many hours as you like. Here's how I run mine with the currently-supported models (as of 9/25/2026) & for my region, hardware:

```
./rtl_433 -f 315M -f 433.92M -H 5 -R 59 -R 60 -R 82 -R 88 -R 89 -R 90 -R 95 -R 110 -R 123 -R 140 -R 156 -R 168 -R 180 -R 186 -R 201 -R 203 -R 208 -R 212 -R 225 -R 226 -R 241 -R 248 -R 252 -R 257 -R 275 -R 295 -R 298 -R 299 -R 321 -R 322 -R 328 -R 343 -R 352 -R 354 -R 355 -R 362 -R 365 -R 378 -R 380 -R 381 -s 1024k -g 42.1 -C customary -F json 2>&1
```

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

... replacing the 0s with any nearby TPMS IDs you logged in your site survey. Then after the `ID.`, input the name of its TPMS, being sure to append it with `TPMSPacket` without a space between, as above.

Any IDs you saw `rtl_433` printing insane false readings for (like hundreds of degrees, or below -40 degrees, where no TPMS should actually be able to operate?) shouldn't get their own extraTemp# line - however, if you do add them, `mintemp.py` should sanity check them away, unless/until sane readings start decoding.

Regardless of you saving them in your configuration, NO TPMS ID IS NOR EVEN CAN BE UPLOADED - only decoded temperatures.

If you've decoded sane TPMS on multiple frequencies, you may set them next to each other with a frequency hopper

eg. `-H 5 -f 315M -f 433.92M`

* Add `user.mintemp.MinTempService` to `data_services = ` under the Engine Services (or if you already added something after `data_services = ` then put it after a comma & space: `, user.mintemp.MinTempService` as the end of the line, no other comma or space). If nothing else, it should be `data_services = user.mintemp.MinTempService` between the `prep_services` & `process_services` lines; don't move it anywhere else.

### Then...

* Save the .py scripts in `weewx/weewx-data/bin/user/`

* Add any TPMS model base identifier string `rtl_433` supports in the future, to `sdr.py` inside `supported_tpms_models = []` using the format already there. As of 9/25/2026, all supported models are included under the `UniversalTPMSPacket` class.

* Set sane bounds for your local climate (using Celsius, NOT Fahrenheit) here, in `mintemp.py`
```
        self.min_valid_temp = float(service_dict.get('min_temp', 0.0))
        self.max_valid_temp = float(service_dict.get('max_temp', 46.0))
```
(originally set for San Diego, CA)

And run: `./weewxd weewx/weewx-data/weewx.conf`