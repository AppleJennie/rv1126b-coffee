export CROSS_COMPILE=/home/lixinkai/rv1126b-v1.1/prebuilts/gcc/linux-x86/aarch64/gcc-arm-10.3-2021.07-x86_64-aarch64-none-linux-gnu/bin/aarch64-none-linux-gnu-
obj-m += chrdevbase.o


KDIR := /home/lixinkai/rv1126b-v1.1/kernel
PWD := $(shell pwd)

all:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean

