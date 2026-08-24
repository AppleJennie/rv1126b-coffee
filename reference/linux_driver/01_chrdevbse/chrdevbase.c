#include <linux/module.h>
#include <linux/fs.h>
#include <linux/init.h>
#include <linux/cdev.h>
#include <linux/uaccess.h>

#define DEVICE_NAME "chrdevbase"
static dev_t devno;
static struct cdev chr_cdev;
static char kernel_buf[100] = "kernel data!";

static int chrdevbase_open(struct inode *inode, struct file *file)
{
    printk("chrdevbase: device opened\n");
    return 0;
}

static int chrdevbase_release(struct inode *inode, struct file *file)
{
    printk("chrdevbase: device closed\n");
    return 0;
}

static ssize_t chrdevbase_read(struct file *file, char __user *buf, size_t len, loff_t *offset)
{
    size_t datalen = strlen(kernel_buf);
    if (len > datalen)
        len = datalen;

    if (copy_to_user(buf, kernel_buf, len))
        return -EFAULT;

    printk("chrdevbase: read %zu bytes\n", len);
    return len;
}

static ssize_t chrdevbase_write(struct file *file, const char __user *buf, size_t len, loff_t *offset)
{
    if (len > sizeof(kernel_buf) - 1)
        len = sizeof(kernel_buf) - 1;

    if (copy_from_user(kernel_buf, buf, len))
        return -EFAULT;

    kernel_buf[len] = '\0';
    printk("chrdevbase: received %zu bytes: %s\n", len, kernel_buf);
    return len;
}

static struct file_operations chrdevbase_fops = {
    .owner = THIS_MODULE,
    .open = chrdevbase_open,
    .release = chrdevbase_release,
    .read = chrdevbase_read,
    .write = chrdevbase_write,
};

static int __init chrdevbase_init(void)
{
    int ret;

    ret = alloc_chrdev_region(&devno, 0, 1, DEVICE_NAME);
    if (ret < 0) {
        printk("chrdevbase: failed to alloc chrdev region\n");
        return ret;
    }

    cdev_init(&chr_cdev, &chrdevbase_fops);
    ret = cdev_add(&chr_cdev, devno, 1);
    if (ret < 0) {
        unregister_chrdev_region(devno, 1);
        printk("chrdevbase: failed to add cdev\n");
        return ret;
    }

    printk("chrdevbase: module loaded. Major: %d Minor: %d\n", MAJOR(devno), MINOR(devno));
    return 0;
}

static void __exit chrdevbase_exit(void)
{
    cdev_del(&chr_cdev);
    unregister_chrdev_region(devno, 1);
    printk("chrdevbase: module unloaded\n");
}

module_init(chrdevbase_init);
module_exit(chrdevbase_exit);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Alientek");
MODULE_DESCRIPTION("Compatible character device driver for Linux 6.6.48");

