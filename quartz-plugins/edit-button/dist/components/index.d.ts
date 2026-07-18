import { QuartzComponent } from '@quartz-community/types';

interface EditButtonOptions {
    /** Text shown on the button. */
    label: string;
}
declare const _default: (userOpts?: Partial<EditButtonOptions>) => QuartzComponent;

export { type EditButtonOptions as E, _default as EditButton };
