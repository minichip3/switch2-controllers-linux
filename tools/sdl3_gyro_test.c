/* Quick SDL3 check: does the ngc gamepad expose IMU sensors?
 * Build on Bazzite: cc -o /tmp/sdl3_gyro_test tools/sdl3_gyro_test.c $(pkg-config --cflags --libs sdl3)
 * Run while nso-gc.service has a controller connected.
 */
#include <SDL3/SDL.h>
#include <stdio.h>

int main(void) {
    if (!SDL_Init(SDL_INIT_GAMEPAD)) {
        fprintf(stderr, "SDL_Init failed: %s\n", SDL_GetError());
        return 1;
    }

    int count = 0;
    SDL_JoystickID *ids = SDL_GetGamepads(&count);
    if (!ids) {
        printf("no gamepads\n");
        SDL_Quit();
        return 0;
    }

    for (int i = 0; i < count; ++i) {
        SDL_Gamepad *gp = SDL_OpenGamepad(ids[i]);
        const char *name = SDL_GetGamepadName(gp);
        int has_gyro = SDL_GamepadHasSensor(gp, SDL_SENSOR_GYRO);
        int has_accel = SDL_GamepadHasSensor(gp, SDL_SENSOR_ACCEL);
        printf("%s  gyro=%d  accel=%d\n", name ? name : "?", has_gyro, has_accel);
        if (has_gyro && SDL_SetGamepadSensorEnabled(gp, SDL_SENSOR_GYRO, true)) {
            float data[3];
            if (SDL_GetGamepadSensorData(gp, SDL_SENSOR_GYRO, data, 3)) {
                printf("  gyro sample: %.3f %.3f %.3f rad/s\n", data[0], data[1], data[2]);
            }
        }
        SDL_CloseGamepad(gp);
    }

    SDL_free(ids);
    SDL_Quit();
    return 0;
}
